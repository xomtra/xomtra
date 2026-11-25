# HAmmond

HAmmond keeps the history of your Home Assistant in its own data lake, finds
correlations, anomalies and lasting changes in it every night and makes all of it
available to Claude and other MCP clients.

## First steps

1. Start the add-on. The first start creates its database and internal keys in the
   add-on's data folder; this takes a minute.
2. Open **HAmmond** in the sidebar (Home Assistant administrators only).
3. On **Tokens**, issue a personal token for each person and device.
4. On **Connect clients**, download the Claude Desktop extension or copy the command for
   Claude Code. The MCP endpoint is port 8000 of your Home Assistant host.

## Requirements

Home Assistant OS or Supervised on an SSD, with at least 4 GB RAM and 20 GB of free
space. Home Assistant Green (32 GB eMMC) and SD cards are not supported.

## More

Documentation: https://github.com/xomtra/HAmmond/blob/main/docs/setup/home-assistant-addon.md
