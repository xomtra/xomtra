# Access from the internet (Claude connector, OAuth)

**Advanced, optional.** Most installations do not need this: Claude Desktop, Claude Code
and other MCP clients at home (or over your VPN) use the
[local access](local-access.md) that the [Docker Compose install](docker.md) sets up
by default. Follow this guide only if you want **claude.ai on the web and on the phone**,
also away from home. Those connect from Anthropic's servers, so xomtra needs a public
`https://` address, a publicly trusted certificate and an OAuth sign-in.

Public access mode (`XOMTRA_ACCESS_MODE=public`): Claude reaches xomtra through
Anthropic's servers and you sign in with a [Home Assistant account](ha-login.md)
(default, ADR 081) or, optionally, with Entra ID (`XOMTRA_AUTH_MODE=entra`,
[guide](entra-app-registration.md)).

## One access mode at a time

An installation runs **either** local **or** public access, never both:

| | Local (default) | Public |
| --- | --- | --- |
| Who connects to `/mcp` | MCP clients in your LAN or VPN (`LAN_CIDR`) | only Claude's servers (`CLAUDE_IP_RANGES`) |
| Sign-in of MCP clients | personal tokens `xmt_…` (page **Tokens**) | OAuth: Claude registers itself, you sign in with your HA account (2FA) |
| Certificate | Caddy's own local CA | Let's Encrypt (ACME DNS-01 at Cloudflare) |
| Reachable from the internet | no | port 443 |
| Admin GUI | `https://<local-host>/admin/` from `ADMIN_CIDR` | `https://<public-host>/admin/` from `ADMIN_CIDR` |

The mode is set **only in `.env`** and applied with
`docker compose up -d --force-recreate`; the admin GUI cannot switch it. The MCP server
starts with one way of checking callers (personal tokens or OAuth) and Caddy with one
configuration (`Caddyfile.local` or `Caddyfile`, chosen in `deploy/caddy/start.sh`).
Accepting personal tokens on a public address would let anyone who obtains a token use
it from the internet, so the two are not mixed.

`XOMTRA_AUTH_MODE` is independent of the access mode and sets who signs in where:

| `XOMTRA_AUTH_MODE` | Local access | Public access |
| --- | --- | --- |
| `ha` (default) | admin GUI: HA administrators; MCP: personal tokens | admin GUI and Claude connector: HA account (connector with 2FA) |
| `entra` | admin GUI: Entra role `xomtra.Admin`; MCP: personal tokens | admin GUI and Claude connector: Entra ID |
| `none` | development only, the server refuses to start unless it listens on loopback | the same |

In the local mode the MCP endpoint always takes personal tokens; `XOMTRA_AUTH_MODE`
only decides the sign-in of the admin GUI there. (The Home Assistant add-on uses its own
fixed combination: admin through HA Ingress, MCP with personal tokens.)

What changes when you switch to public:

- Claude Desktop extensions and Claude Code with personal tokens **stop working**:
  Caddy lets `/mcp` through only from Claude's IP ranges. Use the claude.ai connector
  instead (it also appears in Claude Desktop when you are signed in with the same
  account).
- The page **Tokens** only explains that tokens belong to the local mode; issued tokens
  stay stored and work again if you switch back (unless they expired or you revoked
  them).
- The page **Access** appears: who may use the connector and their sessions.

## 0. Requirements

All of these are required; the details and the reasons are in
[which installation to choose](choose-edition.md#2b-external-connector):

1. A working [Docker Compose installation](docker.md) with Home Assistant connected
   (steps 1–2 there, in the local mode). Start locally, check that it works, then switch.
2. **Your own domain** with its DNS at **Cloudflare** (the free plan is enough) and an
   A record for xomtra pointing to the public IPv4 address of your home network
   (static, or kept up to date by DDNS; not CGNAT).
3. **Port forward** on the router: TCP 443 from the WAN to the machine. No port 80.
4. Home Assistant users who will connect have **two-factor authentication**
   ([HA sign-in](ha-login.md#who-gets-in-adr-081)).
5. A Claude plan with **custom connectors**.

## 1. Publish the server over HTTPS

Traffic path: Internet → router port forward 443 → machine → Caddy (TLS, IP rules) → MCP server.

1. **DNS (Cloudflare):** `A <public-host> → <public-ip>`, proxy status **DNS only**
   (grey cloud). With the orange cloud Caddy would see Cloudflare's addresses instead of
   Claude's and the IP rules could not work.
2. **Cloudflare API token** for the certificate (ACME DNS-01): My Profile → API Tokens →
   Create token → template *Edit zone DNS*, *Zone Resources* = only your zone, optionally
   *Client IP Address Filtering* = your public IP.
3. **Claude's IP ranges:** take the current outbound ranges from Anthropic's
   documentation (*API → IP addresses*, outbound). Re-check them when the connector
   suddenly gets `403`.
4. **Router:** port forward TCP 443 from the WAN to the machine, port 443.
5. **The admin GUI from your LAN:** it moves to `https://<public-host>/admin/` and stays
   limited to `ADMIN_CIDR` (default: the private ranges). Your computer must reach that
   name at the machine: a local DNS record for `<public-host>` pointing to the machine's
   LAN address (router, Pi-hole; recommended), or NAT loopback in the router.

## 2. Switch to the public mode

In `xomtra/deploy/compose`, add to `.env` (create it if you have none; keep the
`*_IMAGE` lines you pinned):

```bash
XOMTRA_ACCESS_MODE=public
XOMTRA_PUBLIC_HOST=<public-host>
ACME_EMAIL=<your e-mail for Let's Encrypt>
CLOUDFLARE_API_TOKEN=<token from step 1.2>
CLAUDE_IP_RANGES=<ranges from step 1.3, space separated>
# Home Assistant as this machine reaches it, the same URL as in the admin GUI
XOMTRA_HA_URL=http://<ha-address>:8123
# optional: limit the admin GUI to your admin machine's network
# ADMIN_CIDR=<admin-cidr>
```

`XOMTRA_HA_URL` is needed for the Home Assistant sign-in of the connector (xomtra
exchanges the sign-in code there); the token can stay in the admin GUI. If your browser
reaches Home Assistant under another address, also set `XOMTRA_HA_AUTH_URL`
([HA sign-in](ha-login.md#settings)). `XOMTRA_BASE_URL` follows from
`XOMTRA_PUBLIC_HOST` (`https://<public-host>`); set it only if it differs. `XOMTRA_LOCAL_HOST` and `LAN_CIDR` are ignored in this mode. Then:

```bash
docker compose up -d --force-recreate
docker compose logs caddy | grep -i "certificate obtained"
```

A missing value stops Caddy with `XOMTRA_ACCESS_MODE=public requires …`. The data, the
Home Assistant connection and the settings stay (they are in the volumes).

What Caddy lets through:

| Path | From | Why |
| --- | --- | --- |
| `/authorize`, `/consent`, `/auth/callback` | anywhere | sign-in steps in **your browser** |
| everything else (`/mcp`, `/token`, `/register`, `/.well-known/*`) | `CLAUDE_IP_RANGES` only | called by Claude's servers |
| `/admin`, `/admin/*` | `ADMIN_CIDR` only, then sign-in with the role `xomtra.Admin` | [admin GUI and Dagster UI](admin-gui.md) |
| `/healthz` | nobody (404) | internal health check only |

The browser paths only start or finish a sign-in; without a successful sign-in of an
allowed user they lead nowhere. Clients that connect from your own machine instead of
Claude's servers (e.g. a local MCP client) are blocked by design.

Checks from outside Claude's ranges (e.g. your phone on mobile data):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<public-host>/mcp        # 403
curl -s -o /dev/null -w '%{http_code}\n' https://<public-host>/authorize          # 400 (reached, no params)
```

## 3. Add the connector in claude.ai

claude.ai → **Settings → Connectors → Add custom connector**

- Name: `xomtra`
- URL: `https://<public-host>/mcp`
- Advanced settings: leave OAuth client ID/secret **empty** (Claude registers itself
  dynamically at xomtra's OAuth proxy).

Click **Connect**: you see xomtra's consent page, then the Home Assistant sign-in (with
2FA; with Entra ID the Microsoft sign-in), then you are back in claude.ai. With Home
Assistant your browser must reach HA's sign-in page while you connect, so connect at
home or over VPN; afterwards the connector works from anywhere
([details](ha-login.md#connect-at-home-use-it-anywhere)).

## 4. Verify

In a new chat with the connector enabled, ask Claude to call `whoami` and
`server_info`. Expected:

- `whoami` shows your name and `xomtra.Admin` in `roles` (HA administrator),
- a Home Assistant user without two-factor authentication, or without a role on the
  page **Access**, cannot finish the sign-in,
- disconnecting the connector and reconnecting works; restarting the `mcp` container
  does not force a new sign-in (stable `XOMTRA_JWT_SIGNING_KEY` + `/data` volume),
- the Caddy access log shows `/mcp` requests only from `CLAUDE_IP_RANGES`.

## Back to local access

Set `XOMTRA_ACCESS_MODE=local` in `.env` (the public values can stay, they are ignored)
and run `docker compose up -d --force-recreate`. Remove the connector in claude.ai, the
port forward and, if you no longer need it, the DNS record and the Cloudflare token.
Clients use personal tokens again; the admin GUI is back at `https://<local-host>/admin/`.
