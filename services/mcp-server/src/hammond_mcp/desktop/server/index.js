#!/usr/bin/env node
// HAmmond bridge for Claude Desktop (ADR 056): MCP over stdio <-> Streamable HTTP.
//
// Claude Desktop starts local MCP servers over stdio only; HAmmond runs on a server in
// the LAN. This bridge forwards every JSON-RPC message to the HAmmond endpoint with the
// personal token and writes the answers back. No dependencies: Node's own http/https,
// so nothing is downloaded at install time.
//
// Environment (set by the extension manifest from the values entered at install):
//   HAMMOND_URL      MCP endpoint, e.g. https://hammond.lan/mcp
//   HAMMOND_TOKEN    personal token from the admin GUI (hmd_...)
//   HAMMOND_CA_FILE  optional PEM file of the server's local CA (self-signed setups)
//   HAMMOND_ALLOW_HTTP  "true" to allow plain http:// to a host other than loopback

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const readline = require("node:readline");

const PROTOCOL_HEADER = "mcp-protocol-version";
const SESSION_HEADER = "mcp-session-id";
const TIMEOUT_MS = 120000;

function log(message) {
  process.stderr.write(`[hammond-bridge] ${message}\n`);
}

function fail(message) {
  log(message);
  process.exit(1);
}

function config(env) {
  const raw = (env.HAMMOND_URL || "").trim();
  const token = (env.HAMMOND_TOKEN || "").trim();
  if (!raw) fail("HAMMOND_URL is not set (MCP endpoint, e.g. https://hammond.lan/mcp)");
  if (!token) fail("HAMMOND_TOKEN is not set (personal token from the HAmmond admin GUI)");
  let url;
  try {
    url = new URL(raw);
  } catch {
    fail(`HAMMOND_URL is not a URL: ${raw}`);
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol === "http:" && !loopback && env.HAMMOND_ALLOW_HTTP !== "true") {
    fail("Plain http:// sends the token unencrypted; use https:// or allow http explicitly");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") fail("HAMMOND_URL must be http(s)");
  let ca;
  const caFile = (env.HAMMOND_CA_FILE || "").trim();
  if (caFile) {
    try {
      ca = fs.readFileSync(caFile);
    } catch (err) {
      fail(`Cannot read HAMMOND_CA_FILE ${caFile}: ${err.message}`);
    }
  }
  return { url, token, ca };
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
  constructor({ url, token, ca }, write) {
    this.url = url;
    this.token = token;
    this.ca = ca;
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
      "user-agent": "hammond-desktop-bridge",
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
      req.on("timeout", () => req.destroy(new Error("HAmmond did not answer in time")));
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
      this.errorFor(message, `Cannot reach HAmmond at ${this.url.origin}: ${err.message}`);
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
        "HAmmond refused the token (revoked, expired or mistyped). Issue a new token in " +
          "the HAmmond admin GUI and enter it in the extension settings.",
      );
      return;
    }
    if (res.status >= 400) {
      this.errorFor(message, `HAmmond answered HTTP ${res.status}`);
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

if (require.main === module) main();

module.exports = { Bridge, config, sseEvents };
