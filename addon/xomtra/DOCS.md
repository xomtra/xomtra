# xomtra

xomtra keeps the history of your Home Assistant in its own data lake, finds
correlations, anomalies and lasting changes in it every night and makes all of it
available to Claude and other MCP clients.

## First steps

1. Start the add-on. The first start creates its database and internal keys in the
   add-on's data folder; this takes a minute.
2. Open **xomtra** in the sidebar (Home Assistant administrators only).
3. On **Tokens**, issue a personal token for each person and device.
4. On **Connect clients**, download the Claude Desktop extension or copy the command for
   Claude Code. The MCP endpoint is port 8000 of your Home Assistant host.

## Requirements

Home Assistant OS or Supervised on an SSD, with at least 4 GB RAM and 20 GB of free
space. Home Assistant Green (32 GB eMMC) and SD cards are not supported.

The **Overview** shows the free space of the disk. Below 10 % free xomtra runs its
retention early; below 3 % it pauses collecting until there is space again and then
catches up from Home Assistant's history.

## Backups

Home Assistant backups do not include xomtra's analytics. They keep the tokens, the
settings and the add-on's database, but not the lake (the history, the findings and the
weekly summaries, tens of GB). After a restore xomtra starts a new lake and loads the
history again from Home Assistant, only as far back as Home Assistant still keeps it.

For a robust setup with backups of the whole history, run xomtra on its own virtual
machine (Proxmox, VMware or Docker on Linux) and back up that machine.

## More

Documentation: https://github.com/xomtra/xomtra/blob/main/docs/setup/home-assistant-addon.md
