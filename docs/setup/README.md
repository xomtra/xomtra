# Setup guides

Order for the proof of concept "Claude connector authenticated by Entra ID":

1. [Host VM](homelab-vm.md) – Debian 13 VM on Proxmox in the DMZ VLAN, Docker.
2. [Entra ID app registration](entra-app-registration.md) – app, API scope, secret, app roles.
3. Deploy the compose stack (`deploy/compose`, see the VM guide).
4. [Claude connector](claude-connector.md) – DNS, port forward, Caddy/TLS and the connector in claude.ai.
5. [Data ingest](ingest.md) – live stream, Dagster jobs, backfill from the running
   Home Assistant (phase 1a.2).

Placeholders used in all guides: `<public-host>` (public DNS name of the MCP server),
`<public-ip>`, `<dmz-cidr>`, `<tenant-id>`, `<client-id>`. Keep real values in `.env` only, never in the repository.
