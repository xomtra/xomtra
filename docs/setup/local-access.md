# Local access (LAN only, personal tokens)

The default access mode of the [Docker Compose install](docker.md) and the right one for
most homes (ADR 056): the MCP endpoint and the admin GUI are
reachable only in your LAN. No public DNS name, certificate authority account, port
forward or cloud identity is needed. Claude Desktop, Claude Code and other MCP clients
on a computer in the LAN (or connected by your VPN) use a **personal token** that you
issue in the admin GUI.

claude.ai on the web and on phones connects through Anthropic's servers and therefore
needs [access from the internet](claude-connector.md) instead, a separate advanced setup.
An installation runs one of the two modes, not both.

## 1. Install

Follow the [Docker Compose install](docker.md), steps 1 and 2: download the release,
`docker compose up -d`, connect Home Assistant with the one-time setup link. Nothing has
to be prepared (ADR 077) and nothing has to be set for the local mode: it is the default
(`XOMTRA_ACCESS_MODE=local`).

`LAN_CIDR` (default: all private ranges) limits who may reach the MCP endpoint.

## 2. Trust the server's certificate

Caddy issues the certificate for `XOMTRA_LOCAL_HOST` from its own local CA.

**Claude Desktop needs nothing from this step:** the extension downloaded from the admin
GUI (step 4) carries the CA certificate and trusts only it. Do this step for Claude
Code, browsers and other clients.

Download the
CA certificate once from `https://<local-host>/xomtra-ca.crt` (your browser warns the
first time, which is expected) and compare its fingerprint with the server:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt \
  | openssl x509 -noout -fingerprint -sha256
```

and on the computer where you downloaded it:

```bash
openssl x509 -noout -fingerprint -sha256 -in ~/Downloads/xomtra-ca.crt   # macOS, Linux
certutil -dump $env:USERPROFILE\Downloads\xomtra-ca.crt | findstr /i "sha256"   # Windows
```

**This step is required for Claude Code** (and for Claude Desktop with an extension
from elsewhere than the admin GUI). It runs on Node, which trusts neither the browser's
exception nor a CA it has not been given; without it, it fails with
`unable to get local issuer certificate`. Do one of the two:

- **Only for Claude** (recommended, no change to the system): start Claude Code with
  `NODE_EXTRA_CA_CERTS=/path/to/xomtra-ca.crt`; in the Claude Desktop extension choose
  `xomtra-ca.crt` as *Certificate of the server*.
- **For the whole computer** (browsers and other clients):

  ```bash
  # macOS
  sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain ~/Downloads/xomtra-ca.crt
  # Windows (PowerShell as administrator)
  certutil -addstore -f Root $env:USERPROFILE\Downloads\xomtra-ca.crt
  # Debian/Ubuntu
  sudo cp xomtra-ca.crt /usr/local/share/ca-certificates/xomtra-ca.crt
  sudo update-ca-certificates
  ```

  Restart Claude Desktop afterwards. Whether the extension uses the system store depends
  on the Node built into Claude Desktop; if it still reports the error, also use the
  first option.

## 3. Issue a token

Open `https://<local-host>/admin/`, sign in (Home Assistant administrator) and go to
**Tokens**: who it is for, a description (device or app), role (`xomtra.Reader` or
`xomtra.Admin`) and expiry. The token (`xmt_…`) is shown **once**, together with ready
instructions for the clients. Only its SHA-256 hash is stored; revoking takes effect
within 30 seconds. Issue one token per person and device, so that you can revoke one
without the others and the audit shows who asked what.

## 4. Connect a client

### Claude Desktop

1. Admin GUI → **Tokens** → issue a token → **Download for Claude Desktop**
   (`xomtra.mcpb`, also on **Connect clients**). The file is made for this server: the
   address and the certificate of its local CA are filled in, and the extension trusts
   only this certificate (pinned) instead of the computer's trust store. Nothing has
   to be imported into the system.
2. Open the file; Claude Desktop shows the extension and asks for the **personal
   token**. Paste it and install.

The token is deliberately not part of the file: it would be a password lying in the
Downloads folder, in backups and in anything the file is sent with, and anyone with
the file could use it. Claude Desktop stores the token you paste as a secret. The
address and the certificate in the file are public.

After a new installation of the server (a new local CA) the extension reports that
the server has a new certificate authority: download it again from the admin GUI.
*Certificate of the server* in the extension settings stays available and wins over
the bundled certificate.

The extension is a small bridge without dependencies (`server/index.js`) that forwards
MCP from Claude Desktop to the xomtra endpoint. It refuses plain `http://` to anything
but the local machine unless you allow it explicitly.

### Claude Code

```bash
claude mcp add --transport http xomtra https://<local-host>/mcp \
  --header "Authorization: Bearer xmt_..."
```

With the server's own CA not in the system trust store, start Claude Code with
`NODE_EXTRA_CA_CERTS=/path/to/xomtra-ca.crt`.

### Other MCP clients

Any client that speaks MCP over Streamable HTTP and can send a header:

```json
{
  "mcpServers": {
    "xomtra": {
      "type": "http",
      "url": "https://<local-host>/mcp",
      "headers": { "Authorization": "Bearer xmt_..." }
    }
  }
}
```

Clients that only speak stdio can use the same bridge as Claude Desktop:
`XOMTRA_URL=https://<local-host>/mcp XOMTRA_TOKEN=xmt_... node server/index.js` (from
the unpacked `xomtra.mcpb`, which trusts the certificate it carries; `XOMTRA_CA_FILE`
for another CA certificate).

## What Caddy lets through (local mode)

| Path | From | Why |
| --- | --- | --- |
| `/mcp` | `LAN_CIDR`, then a valid personal token | MCP clients |
| `/xomtra-ca.crt` | `LAN_CIDR` | root certificate of the local CA |
| `http://caddy:8081/xomtra-ca.crt` | compose network only (port not published) | the same certificate for the admin GUI, which puts it into the Claude Desktop extension |
| `/admin`, `/admin/*` | `ADMIN_CIDR`, then sign-in with the admin role | admin GUI, Dagster UI |
| everything else | nobody (404) | |

## Audit

Every tool call is audited as before (ADR 040), with `idp = token`, the token id as
subject and the person it was issued for as username. Issuing and revoking tokens are
audited as `admin:token_issue` and `admin:token_revoke` (without the token itself).
