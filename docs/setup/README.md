# Setup guides (phase 1a.1)

Order for the proof of concept "Claude connector authenticated by Entra ID":

1. [Host VM](homelab-vm.md) – Debian 13 VM on Proxmox in its own VLAN, Docker.
2. [Entra ID app registration](entra-app-registration.md) – app, API scope, secret, app roles.
3. Deploy the compose stack (`deploy/compose`, see the VM guide).
4. [Claude connector](claude-connector.md) – expose the server and add it in claude.ai.

Placeholders used in all guides: `<public-host>` (public DNS name of the MCP server),
`<tenant-id>`, `<client-id>`. Keep real values in `.env` only, never in the repository.
