# Setup guides

Not sure which installation fits? See [which installation to choose](choose-edition.md):
limits of the add-on and of your own machine, which AI assistants connect, and what the
external Claude connector needs (domain, A record, port 443, certificate, sign-in).

Home Assistant add-on (lite edition, in development): [home-assistant-addon.md](home-assistant-addon.md).

## A. Local access (default, most homes)

LAN or VPN only, personal tokens for Claude Desktop, Claude Code and other MCP clients.

1. [Docker Compose install](docker.md) – `docker compose up -d`, then connect Home
   Assistant in the browser (ADR 077).
2. [Local access](local-access.md) – trust the certificate, issue a token, connect the
   clients. The admin GUI uses a [Home Assistant account](ha-login.md).

Backups and restore: [backup.md](backup.md). To remove it again completely:
[uninstall](uninstall.md).

## B. Access from the internet (advanced, optional)

Only for claude.ai on the web and on the phone. It replaces A on the same installation
(one access mode at a time, switched in `.env`).

1. A working installation from A (step 1).
2. [Claude connector](claude-connector.md) – domain at Cloudflare, port forward, Claude's
   IP ranges, the switch to `XOMTRA_ACCESS_MODE=public` and the connector in claude.ai.
   Sign-in with a [Home Assistant account](ha-login.md) (default) or optionally
   [Entra ID](entra-app-registration.md).
3. Optional: a [hardened host VM](homelab-vm.md) in a DMZ (Proxmox, Debian 13).

## Reference

- [Data ingest](ingest.md) – live stream, Dagster jobs, backfill from the running
  Home Assistant.
- [Catalog and MCP data tools](mcp-tools.md) – catalog, tools, roles and policy, SQL
  limits, audit.
- [Admin GUI and Dagster UI](admin-gui.md).

Placeholders used in all guides: `<public-host>` (public DNS name of the MCP server),
`<local-host>` (name or IP of the server in the LAN),
`<public-ip>`, `<dmz-cidr>`, `<tenant-id>`, `<client-id>`. Keep real values in `.env` only, never in the repository.
