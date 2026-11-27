# Setup guides

Not sure which installation fits? See [which installation to choose](choose-edition.md):
limits of the add-on and of your own machine, which AI assistants connect, and what the
external Claude connector needs (domain, A record, port 443, certificate, sign-in).

Quick start: [Docker Compose](docker.md) – `docker compose up -d`, then connect Home
Assistant in the browser (ADR 077); [local access](local-access.md) – LAN only,
personal tokens for Claude Desktop, Claude Code and other MCP clients, sign-in to the
admin GUI with a [Home Assistant account](ha-login.md). To remove it again completely:
[uninstall](uninstall.md).

Home Assistant add-on (lite edition, in development): [home-assistant-addon.md](home-assistant-addon.md).

Order for the public setup "Claude connector authenticated by Entra ID" (homelab):

1. [Host VM](homelab-vm.md) – Debian 13 VM on Proxmox in the DMZ VLAN, Docker.
2. [Entra ID app registration](entra-app-registration.md) – app, API scope, secret, app roles.
3. Deploy the compose stack (`deploy/compose`, see the VM guide).
4. [Claude connector](claude-connector.md) – DNS, port forward, Caddy/TLS and the connector in claude.ai.
5. [Data ingest](ingest.md) – live stream, Dagster jobs, backfill from the running
   Home Assistant (phase 1a.2).
6. [Catalog and MCP data tools](mcp-tools.md) – catalog, tools, roles and policy, SQL
   limits, audit (phase 1a.3).
7. [Admin GUI and Dagster UI](admin-gui.md) – from the admin network with Entra role
   `xomtra.Admin` (phase 1a.3).

Instead of Entra ID, the connector and the admin GUI can use
[Home Assistant accounts](ha-login.md) (`XOMTRA_AUTH_MODE=ha`, ADR 053).

Placeholders used in all guides: `<public-host>` (public DNS name of the MCP server),
`<local-host>` (name or IP of the server in the LAN),
`<public-ip>`, `<dmz-cidr>`, `<tenant-id>`, `<client-id>`. Keep real values in `.env` only, never in the repository.
