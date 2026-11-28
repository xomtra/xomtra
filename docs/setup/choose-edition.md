# Which installation to choose

Two ways to run xomtra, and two ways to connect an AI assistant. Pick by the size of your
home, how much history you want and where you want to ask Claude from.

| I want | Choose |
| --- | --- |
| to start in 10 minutes, a smaller home, history up to about a year, Claude Desktop or Claude Code at home | [Home Assistant add-on](home-assistant-addon.md) |
| years of history, a large home, full backups | [own machine](docker.md), local connector |
| Claude on the web and on the phone, also away from home | [own machine](docker.md), external connector |

## 1. Home Assistant add-on (lite edition)

| | |
| --- | --- |
| Data | small to medium installations; as a guide up to about 1000 recorded series (measured: 1000 series × 365 days ≈ 1.7 GiB peak memory of the analysis) |
| History | states and 5-minute statistics 90 days by default, **at most about one year**; hourly statistics 5 years |
| Hardware | Home Assistant OS or Supervised, SSD, at least 4 GB RAM and 20 GB free space, amd64 or aarch64. Not Home Assistant Green (eMMC), not SD cards |
| Backups | Home Assistant backups hold tokens, settings and audit, **not the history**; after a restore xomtra loads the history again as far back as Home Assistant still has it |
| AI connection | **local network only (or over VPN), over HTTP**: `http://<ha-host>:8000/mcp` with a personal token `xmt_…` |
| Sign-in | admin pages through Home Assistant (Ingress); AI clients with personal tokens |

AI assistants that can connect (they run on a computer in your network):

| Assistant | Status |
| --- | --- |
| Claude Desktop (extension `xomtra.mcpb` from the admin pages) | tested |
| Claude Code (`claude mcp add --transport http … --header "Authorization: Bearer …"`) | tested |
| VS Code (GitHub Copilot, agent mode), Cursor | supported (Streamable HTTP and a header), not tested in CI |
| other MCP clients with Streamable HTTP and a custom header (e.g. Windsurf, Cline, Codex CLI, Gemini CLI) | should work, not tested |
| **not possible:** Claude on the web and on phones (claude.ai), ChatGPT connectors, other cloud assistants | they connect from their provider's servers and cannot reach your network |

The token travels unencrypted inside your network (HTTP). Away from home use a VPN
(WireGuard, Tailscale or similar); never forward port 8000 to the internet.

## 2. Own machine (Docker Compose; Proxmox and VMware images later)

| | |
| --- | --- |
| Data | large installations; limited by the machine's memory and disk |
| History | configurable, several years; limited by the disk only |
| Hardware | a VM or computer with Docker, 8 GB RAM recommended, SSD, disk by history |
| Backups | back up the whole machine or its volumes, including the history |

### 2a. Local connector

The default. As with the add-on: local network or VPN, personal tokens, the same
assistants, but over HTTPS with xomtra's own certificate authority
(`XOMTRA_ACCESS_MODE=local`, see [local access](local-access.md)). Nothing else needed.

### 2b. External connector

`XOMTRA_ACCESS_MODE=public`, advanced and optional: Claude on the web, on the desktop
and on the phone. It replaces the local connector (an installation runs one of the two;
personal tokens then stop working) and is switched in `.env`. **All of these are
required:**

1. **Your own domain** with an **A record** (e.g. `xomtra.<your-domain>`) pointing to the
   public IP address of your home network.
   * **Static IP:** set the A record once.
   * **Dynamic IP:** a DDNS client updates the A record when the address changes (many
     routers have one, or a small container for Cloudflare DDNS); keep the TTL short
     (60–300 s). After a change the connector fails until the record is updated.
   * **A public IPv4 address, not CGNAT.** If the router's WAN address is in
     100.64.0.0/10 or differs from the address the internet sees, port forwarding cannot
     work.
2. **Port forwarding** on the router: TCP 443 from the WAN to the machine. Port 80 stays
   closed.
3. **A certificate** (below): publicly trusted. Self-signed certificates or your own CA
   do **not** work, Claude's servers refuse them.
4. **Sign-in with a Home Assistant account** (default, [guide](ha-login.md)) or,
   optionally, Microsoft Entra ID ([guide](entra-app-registration.md)). With Home
   Assistant, the browser must reach Home Assistant's sign-in page **when you connect
   the connector**: at home or over VPN, or always if Home Assistant is reachable from
   the internet (Home Assistant Cloud or your own reverse proxy). After that the
   connector works from anywhere. Home Assistant users need two-factor authentication.
5. **A Claude plan with custom connectors**, as Anthropic currently offers.

Assistants: Claude (web, desktop, iOS, Android) as a custom connector. **ChatGPT and other
cloud assistants are not supported yet:** Caddy lets `/mcp` through only from Claude's
IP ranges.

#### Certificate: what you set up once

Caddy obtains and renews the certificate itself (Let's Encrypt, ACME DNS-01).

| # | What | Why / watch out |
| --- | --- | --- |
| 1 | **The domain's DNS at Cloudflare** (the free plan is enough) | the `xomtra-caddy` image has only the `caddy-dns/cloudflare` plugin; other DNS providers are not supported yet |
| 2 | **A Cloudflare API token** with *Zone → DNS → Edit*, limited **to this zone only** (optionally also to your public IP) → `.env` `CLOUDFLARE_API_TOKEN` | Caddy uses it to create the `_acme-challenge` record at every issue and renewal; do not delete it |
| 3 | The A record **DNS only** (grey cloud), not proxied | behind Cloudflare's proxy Caddy would not see Claude's addresses and the IP filter could not work |
| 4 | If the domain has a **CAA** record, it must allow `letsencrypt.org` | otherwise issuing fails |
| 5 | `XOMTRA_PUBLIC_HOST` / `XOMTRA_BASE_URL` = exactly this name, `ACME_EMAIL` | the certificate is for this name; Let's Encrypt writes to the e-mail address |
| 6 | **Keep the volume `caddy_data`** (do not delete it on upgrades) | without it a certificate is issued at every start and runs into Let's Encrypt's limits (5 identical certificates per week) |
| 7 | Correct time on the machine (NTP) | otherwise TLS and tokens fail |
| 8 | Outgoing HTTPS from the machine to Let's Encrypt and the Cloudflare API | firewall or DMZ rules must allow outgoing 443 |

Renewal is automatic. Check: `docker compose logs caddy` shows `certificate obtained`;
from mobile data, `curl -I https://<public-host>/authorize` must show a valid certificate.
Details: [Claude connector](claude-connector.md).
