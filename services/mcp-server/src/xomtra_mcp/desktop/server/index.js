#!/usr/bin/env node
// xomtra bridge for Claude Desktop (ADR 056): MCP over stdio <-> Streamable HTTP.
//
// Claude Desktop starts local MCP servers over stdio only; xomtra runs on a server in
// the LAN. This bridge forwards every JSON-RPC message to the xomtra endpoint with the
// personal token and writes the answers back. No dependencies: Node's own http/https,
// so nothing is downloaded at install time.
//
// Environment (set by the extension manifest from the values entered at install):
//   XOMTRA_URL      MCP endpoint, e.g. https://xomtra.lan/mcp
//   XOMTRA_TOKEN    personal token from the admin GUI (xmt_...)
//   XOMTRA_CA_FILE  optional PEM file of the server's local CA (self-signed setups)
//
// An extension downloaded from the admin GUI of a server with its own CA carries that
// CA as server/xomtra-ca.crt; without XOMTRA_CA_FILE the bridge then trusts only it
// (pinned) instead of the computer's trust store.
//   XOMTRA_ALLOW_HTTP  "true" to allow plain http:// to a host other than loopback

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const path = require("node:path");
const readline = require("node:readline");

const BUNDLED_CA = path.join(__dirname, "xomtra-ca.crt");

const PROTOCOL_HEADER = "mcp-protocol-version";
const SESSION_HEADER = "mcp-session-id";
const TIMEOUT_MS = 120000;
// Claude Desktop gives up on a server after 60 s without a word; a connection that
// does not open (on macOS: Claude not allowed in Local Network) must fail long before.
const CONNECT_TIMEOUT_MS = Number(process.env.XOMTRA_CONNECT_TIMEOUT_MS) || 10000;
const UNREACHABLE_HINT =
  "Check the address and that this computer reaches the server. On macOS allow Claude " +
  "in System Settings > Privacy & Security > Local Network, then restart Claude.";

// Node's codes for a certificate chain this computer does not trust.
const UNTRUSTED_CODES = new Set([
  "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
  "UNABLE_TO_GET_ISSUER_CERT",
  "SELF_SIGNED_CERT_IN_CHAIN",
  "DEPTH_ZERO_SELF_SIGNED_CERT",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "CERT_SIGNATURE_FAILURE",
]);

// What to do about a failed connection: the certificate, or reaching the server.
function hintFor(err, url, caSource) {
  const code = err && err.code;
  if (UNTRUSTED_CODES.has(code)) {
    if (caSource === "bundled") {
      return (
        "The server has a new certificate authority since this extension was downloaded " +
        "(for example after a new installation). Download the extension again from the " +
        "xomtra admin GUI (Tokens) and install it."
      );
    }
    if (caSource === "chosen") {
      return (
        "The certificate file chosen in the extension settings is not the one of this " +
        `server. Download it again from ${url.origin}/xomtra-ca.crt and choose that file.`
      );
    }
    return (
      "This computer does not trust the certificate of the server (xomtra's own local " +
      `certificate authority). Download ${url.origin}/xomtra-ca.crt and choose it in ` +
      "the extension settings under Certificate of the server, or trust it in the " +
      "system (docs/setup/local-access.md, also how to check its fingerprint)."
    );
  }
  if (code === "ERR_TLS_CERT_ALTNAME_INVALID") {
    return (
      "The certificate of the server is for another name. Use the address the server " +
      "was set up with (XOMTRA_LOCAL_HOST), as shown on Connect in the xomtra admin GUI."
    );
  }
  return UNREACHABLE_HINT;
}

function log(message) {
  process.stderr.write(`[xomtra-bridge] ${message}\n`);
}

function fail(message) {
  log(message);
  process.exit(1);
}

function config(env) {
  const raw = (env.XOMTRA_URL || "").trim();
  const token = (env.XOMTRA_TOKEN || "").trim();
  if (!raw) fail("XOMTRA_URL is not set (MCP endpoint, e.g. https://xomtra.lan/mcp)");
  if (!token) fail("XOMTRA_TOKEN is not set (personal token from the xomtra admin GUI)");
  let url;
  try {
    url = new URL(raw);
  } catch {
    fail(`XOMTRA_URL is not a URL: ${raw}`);
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol === "http:" && !loopback && env.XOMTRA_ALLOW_HTTP !== "true") {
    fail("Plain http:// sends the token unencrypted; use https:// or allow http explicitly");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") fail("XOMTRA_URL must be http(s)");
  let ca;
  let caSource = null;
  const caFile = (env.XOMTRA_CA_FILE || "").trim();
  if (caFile) {
    try {
      ca = fs.readFileSync(caFile);
      caSource = "chosen";
    } catch (err) {
      fail(`Cannot read XOMTRA_CA_FILE ${caFile}: ${err.message}`);
    }
  } else if (url.protocol === "https:" && fs.existsSync(BUNDLED_CA)) {
    ca = fs.readFileSync(BUNDLED_CA);
    caSource = "bundled";
  }
  return { url, token, ca, caSource };
}

// Minimal Server-Sent Events parser: yields the data of each event.
function* sseEvents(buffer) {
  const blocks = buffer.split(/\r?\n\r?\n/);
  for (const block of blocks) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (data) yield data;
  }
}

class Bridge {
  constructor({ url, token, ca, caSource }, write) {
    this.url = url;
    this.token = token;
    this.ca = ca;
    this.caSource = caSource || null;
    this.write = write;
    this.session = null;
    this.protocol = null;
    this.initialize = null; // last initialize request, replayed if the session expires
    this.ready = Promise.resolve();
  }

  post(body, method = "POST") {
    const lib = this.url.protocol === "https:" ? https : http;
    const headers = {
      accept: "application/json, text/event-stream",
      authorization: `Bearer ${this.token}`,
      "user-agent": "xomtra-desktop-bridge",
    };
    if (body !== undefined) headers["content-type"] = "application/json";
    if (this.session) headers[SESSION_HEADER] = this.session;
    if (this.protocol) headers[PROTOCOL_HEADER] = this.protocol;
    const options = { method, headers, timeout: TIMEOUT_MS };
    if (this.ca) options.ca = this.ca;
    return new Promise((resolve, reject) => {
      const req = lib.request(this.url, options, (res) => {
        const sse = (res.headers["content-type"] || "").includes("text/event-stream");
        let pending = "";
        const messages = [];
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (!sse) {
            pending += chunk;
            return;
          }
          // Forward complete events as they arrive (progress, long tool calls).
          pending += chunk;
          const cut = Math.max(pending.lastIndexOf("\n\n"), pending.lastIndexOf("\r\n\r\n"));
          if (cut >= 0) {
            for (const data of sseEvents(pending.slice(0, cut))) messages.push(data);
            pending = pending.slice(cut).replace(/^\r?\n\r?\n/, "");
            this.flush(messages);
          }
        });
        res.on("end", () => {
          if (sse) {
            for (const data of sseEvents(pending)) messages.push(data);
            this.flush(messages);
            resolve({ status: res.statusCode, headers: res.headers, text: "" });
          } else {
            resolve({ status: res.statusCode, headers: res.headers, text: pending });
          }
        });
        res.on("error", reject);
      });
      req.on("socket", (socket) => {
        if (!socket.connecting) return; // a kept-alive connection
        const timer = setTimeout(
          () => req.destroy(new Error(`no connection within ${CONNECT_TIMEOUT_MS / 1000} s`)),
          CONNECT_TIMEOUT_MS,
        );
        socket.once("connect", () => clearTimeout(timer));
        socket.once("close", () => clearTimeout(timer));
      });
      req.on("timeout", () => req.destroy(new Error("xomtra did not answer in time")));
      req.on("error", reject);
      if (body !== undefined) req.write(JSON.stringify(body));
      req.end();
    });
  }

  flush(messages) {
    while (messages.length) this.write(messages.shift());
  }

  remember(res, message) {
    const session = res.headers[SESSION_HEADER];
    if (session) this.session = session;
    if (message && message.method === "initialize" && res.text) {
      try {
        const reply = JSON.parse(res.text);
        if (reply.result && reply.result.protocolVersion) {
          this.protocol = reply.result.protocolVersion;
        }
      } catch {
        // not JSON: handled by the caller
      }
    }
  }

  errorFor(message, text) {
    if (message.id === undefined || message.id === null) {
      log(text);
      return;
    }
    this.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32000, message: text } }));
  }

  async send(message, retried = false) {
    let res;
    try {
      res = await this.post(message);
    } catch (err) {
      this.errorFor(
        message,
        `Cannot reach xomtra at ${this.url.origin}: ${err.message}. ` +
          hintFor(err, this.url, this.caSource),
      );
      return;
    }
    if (res.status === 404 && this.session && !retried && this.initialize) {
      // The server forgot the session (restart): start a new one and retry once.
      log("Session expired, reconnecting");
      this.session = null;
      await this.send(this.initialize, true).catch(() => {});
      await this.post({ jsonrpc: "2.0", method: "notifications/initialized" }).catch(() => {});
      return this.send(message, true);
    }
    if (res.status === 401 || res.status === 403) {
      this.errorFor(
        message,
        "xomtra refused the token (revoked, expired or mistyped). Issue a new token in " +
          "the xomtra admin GUI and enter it in the extension settings.",
      );
      return;
    }
    if (res.status >= 400) {
      this.errorFor(message, `xomtra answered HTTP ${res.status}`);
      return;
    }
    this.remember(res, message);
    if (res.text) {
      const reply = JSON.parse(res.text);
      for (const item of Array.isArray(reply) ? reply : [reply]) {
        if (retried && message === this.initialize) continue; // replay: answer already sent
        this.write(JSON.stringify(item));
      }
    }
  }

  handle(line) {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      log("Ignoring a line that is not JSON");
      return;
    }
    if (message.method === "initialize") {
      this.initialize = message;
      // Everything else waits until the session exists.
      this.ready = this.send(message);
      return;
    }
    this.ready.then(() => this.send(message)).catch((err) => this.errorFor(message, err.message));
  }

  async close() {
    if (!this.session) return;
    await this.ready.catch(() => {});
    await this.post(undefined, "DELETE").catch(() => {});
  }
}

function main() {
  const bridge = new Bridge(config(process.env), (text) => process.stdout.write(`${text}\n`));
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", (line) => bridge.handle(line));
  rl.on("close", () => {
    bridge.close().finally(() => process.exit(0));
  });
}

// Always start: Claude Desktop runs the extension in its built-in Node (an Electron
// utility process), where require.main is not this module.
main();
