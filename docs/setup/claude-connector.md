# Claude connector (PoC)

## 1. Publish the server over HTTPS

Traffic path: Internet → router port forward 443 → VM → Caddy (TLS, IP rules) → MCP server.

1. **DNS (Cloudflare):** `A <public-host> → <public-ip>`, proxy status **DNS only**
   (grey cloud). With the orange cloud Caddy would see Cloudflare's addresses instead of
   Claude's and the IP rules could not work.
2. **Cloudflare API token** for the certificate (ACME DNS-01): My Profile → API Tokens →
   Create token → template *Edit zone DNS*, *Zone Resources* = only your zone, optionally
   *Client IP Address Filtering* = your public IP. → `.env`: `CLOUDFLARE_API_TOKEN`.
3. **Claude's IP ranges:** take the current outbound ranges from Anthropic's
   documentation (*API → IP addresses*, outbound) → `.env`: `CLAUDE_IP_RANGES`
   (space separated). Re-check them when the connector suddenly gets `403`.
4. **Router:** port forward TCP 443 from the WAN to the VM, port 443. No port 80.
5. `.env`: `HAMMOND_PUBLIC_HOST=<public-host>`, `HAMMOND_BASE_URL=https://<public-host>`,
   `ACME_EMAIL`; Entra redirect URI `https://<public-host>/auth/callback`.
6. `docker compose up -d` and check the certificate in `docker compose logs caddy`.

What Caddy lets through:

| Path | From | Why |
| --- | --- | --- |
| `/authorize`, `/consent`, `/auth/callback` | anywhere | sign-in steps in **your browser** |
| everything else (`/mcp`, `/token`, `/register`, `/.well-known/*`) | `CLAUDE_IP_RANGES` only | called by Claude's servers |
| `/admin`, `/admin/*` | `ADMIN_CIDR` only, then Entra role `HAmmond.Admin` | [admin GUI and Dagster UI](admin-gui.md) |
| `/healthz` | nobody (404) | internal health check only |

The browser paths only start or finish an Entra sign-in; without a successful sign-in of
an assigned user they lead nowhere. Clients that connect from your own machine instead
of Claude's servers (e.g. a local MCP client) are blocked by design.

Checks from outside Claude's ranges (e.g. your phone on mobile data):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<public-host>/mcp        # 403
curl -s -o /dev/null -w '%{http_code}\n' https://<public-host>/authorize          # 400 (reached, no params)
```

## 2. Add the connector in claude.ai

claude.ai → **Settings → Connectors → Add custom connector**

- Name: `HAmmond`
- URL: `https://<public-host>/mcp`
- Advanced settings: leave OAuth client ID/secret **empty** (Claude registers itself
  dynamically at HAmmond's OAuth proxy).

Click **Connect**: you see HAmmond's consent page, then the Entra sign-in (MFA per your
Conditional Access), then you are back in claude.ai.

## 3. Verify (PoC acceptance)

In a new chat with the connector enabled, ask Claude to call `whoami` and
`server_info` (and `ha_overview` if Home Assistant is configured). Expected:

- `whoami` shows your name, `HAmmond.Admin` in `roles` and the `mcp.access` scope,
- a user without assignment in the enterprise app cannot finish the sign-in,
- disconnecting the connector and reconnecting works; restarting the `mcp` container
  does not force a new sign-in (stable `HAMMOND_JWT_SIGNING_KEY` + `/data` volume),
- the Caddy access log shows `/mcp` requests only from `CLAUDE_IP_RANGES`.

Report the result (works / error message) in the project, so the decision about the
OAuth proxy (risk check of phase 1a) can be closed.
