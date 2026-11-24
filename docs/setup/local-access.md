# Local access (LAN only, personal tokens)

The quickest way to try HAmmond (ADR 056): the MCP endpoint and the admin GUI are
reachable only in your LAN. No public DNS name, certificate authority account, port
forward or cloud identity is needed. Claude Desktop, Claude Code and other MCP clients
on a computer in the LAN (or connected by your VPN) use a **personal token** that you
issue in the admin GUI.

claude.ai on the web and on phones connects through Anthropic's servers and therefore
needs the [public access mode](claude-connector.md) instead.

## 1. Install

```bash
cd deploy/compose
./init-env.sh          # generates every internal password and key (ADR 061)
docker compose up -d
```

`init-env.sh` asks only for what it cannot know (answers can also be passed as
environment variables, see the script header):

| Question | Example | Written to `.env` |
| --- | --- | --- |
| Access mode | `local` | `HAMMOND_ACCESS_MODE` |
| Name or IP of this server | `hammond.lan` or `192.168.1.20` | `HAMMOND_LOCAL_HOST`, `HAMMOND_BASE_URL` |
| Sign-in | `ha` | `HAMMOND_AUTH_MODE` ([Home Assistant account](ha-login.md)) |
| Network of the admin GUI | `192.168.1.0/24` | `ADMIN_CIDR` |
| Home Assistant URL and token | `http://homeassistant.local:8123` | `HAMMOND_HA_URL`, `HAMMOND_HA_TOKEN` ([ingest](ingest.md)) |

The Postgres, S3, OAuth and session secrets are generated, stored only in `.env`
(mode 600) and never need to be typed or copied. After an update, `./init-env.sh --fill`
adds settings that are new in the template and generates missing secrets; it never
changes existing values.

`LAN_CIDR` (default: all private ranges) limits who may reach the MCP endpoint.

## 2. Trust the server's certificate

Caddy issues the certificate for `HAMMOND_LOCAL_HOST` from its own local CA. Download the
CA certificate once from `https://<local-host>/hammond-ca.crt` (your browser warns the
first time, which is expected) and compare its fingerprint with the server:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt \
  | openssl x509 -noout -fingerprint -sha256
```

Then either import it into the system's trust store (browser and every client trust the
server) or give it only to the clients that need it (below).

## 3. Issue a token

Open `https://<local-host>/admin/`, sign in (Home Assistant administrator) and go to
**Tokens**: who it is for, a description (device or app), role (`HAmmond.Reader` or
`HAmmond.Admin`) and expiry. The token (`hmd_…`) is shown **once**, together with ready
instructions for the clients. Only its SHA-256 hash is stored; revoking takes effect
within 30 seconds. Issue one token per person and device, so that you can revoke one
without the others and the audit shows who asked what.

## 4. Connect a client

### Claude Desktop

1. Admin GUI → **Tokens → How to connect** → download `hammond.mcpb`. The server's
   address is already filled in.
2. Open the file; Claude Desktop shows the extension and asks for the **personal
   token** (stored as a secret by Claude Desktop) and, if the CA is not in your system
   trust store, for the **certificate of the server** (`hammond-ca.crt`).

The extension is a small bridge without dependencies (`server/index.js`) that forwards
MCP from Claude Desktop to the HAmmond endpoint. It refuses plain `http://` to anything
but the local machine unless you allow it explicitly.

### Claude Code

```bash
claude mcp add --transport http hammond https://<local-host>/mcp \
  --header "Authorization: Bearer hmd_..."
```

With the server's own CA not in the system trust store, start Claude Code with
`NODE_EXTRA_CA_CERTS=/path/to/hammond-ca.crt`.

### Other MCP clients

Any client that speaks MCP over Streamable HTTP and can send a header:

```json
{
  "mcpServers": {
    "hammond": {
      "type": "http",
      "url": "https://<local-host>/mcp",
      "headers": { "Authorization": "Bearer hmd_..." }
    }
  }
}
```

Clients that only speak stdio can use the same bridge as Claude Desktop:
`HAMMOND_URL=https://<local-host>/mcp HAMMOND_TOKEN=hmd_... node server/index.js` (from
the unpacked `hammond.mcpb`; `HAMMOND_CA_FILE` for the CA certificate).

## What Caddy lets through (local mode)

| Path | From | Why |
| --- | --- | --- |
| `/mcp` | `LAN_CIDR`, then a valid personal token | MCP clients |
| `/hammond-ca.crt` | `LAN_CIDR` | root certificate of the local CA |
| `/admin`, `/admin/*` | `ADMIN_CIDR`, then sign-in with the admin role | admin GUI, Dagster UI |
| everything else | nobody (404) | |

## Audit

Every tool call is audited as before (ADR 040), with `idp = token`, the token id as
subject and the person it was issued for as username. Issuing and revoking tokens are
audited as `admin:token_issue` and `admin:token_revoke` (without the token itself).
