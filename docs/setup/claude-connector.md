# Claude connector (PoC)

## 1. Publish the server over HTTPS

PoC (iterations 1a.1–1a.2): Tailscale Funnel on the VM, nothing opened on the router.

```bash
sudo tailscale up
sudo tailscale funnel --bg 8000
tailscale funnel status          # shows https://<machine>.<tailnet>.ts.net
```

Set `HAMMOND_BASE_URL=https://<machine>.<tailnet>.ts.net` in `.env`, add
`https://<machine>.<tailnet>.ts.net/auth/callback` as redirect URI of the Entra app,
then `docker compose up -d mcp`.

Funnel makes the endpoint reachable from the Internet; it is protected only by OAuth.
Check that it refuses unauthenticated calls:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<public-host>/mcp   # 401
curl -s https://<public-host>/.well-known/oauth-protected-resource/mcp
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
  does not force a new sign-in (stable `HAMMOND_JWT_SIGNING_KEY` + `/data` volume).

Report the result (works / error message) in the project, so the decision about the
OAuth proxy (risk check of phase 1a) can be closed.
