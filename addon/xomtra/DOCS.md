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

## What xomtra can do in Home Assistant

xomtra only reads data: registries, states, history and statistics. It never calls
services and changes nothing in Home Assistant. Your AI assistant talks only to xomtra
and gets read-only tools over xomtra's own copy of the data, never access to
controlling your home. Home Assistant itself has no read-only tokens (the add-on's
token could do more), so this is guaranteed by xomtra's open source code, not by a
Home Assistant permission.

## Style of the answers

xomtra gives your assistant guidelines for answering: by default the set **Default**,
which looks for the relevant data first, answers first and then shows the evidence as a
chart or a table, in a friendly tone. On the admin page **Answers** you can activate
another set (**Concise**, or **Core only** for no guidelines at all) or duplicate one and
write your own; several named sets can be kept and one is active. A change applies within
30 seconds, without a restart. The sets are kept in the add-on's database (and in Home
Assistant backups); nothing is sent from xomtra.com.

Core rules always apply and cannot be changed: no jokes about accidents, outages, risky
situations or threats to life and health; nothing sexist, racist, homophobic, xenophobic,
political or religious; no conclusions about a person's presence, sleep, health or
habits unless asked; only values fetched from xomtra; and xomtra only reads data.

## Requirements

Home Assistant OS or Supervised on an SSD, with at least 4 GB RAM and 20 GB of free
space. Home Assistant Green (32 GB eMMC) and SD cards are not supported.

The **Overview** shows the free space of the disk. Below 10 % free xomtra runs its
retention early; below 3 % it pauses collecting until there is space again and then
catches up from Home Assistant's history.

The retention periods (state history, 5-minute and hourly statistics) are set on the
page **Retention** of xomtra's admin. The add-on's options of the same name are only the
default; a value saved in the admin wins, applies from the next nightly run and needs no
restart.

## Backups

Home Assistant backups do not include xomtra's analytics. They keep the tokens, the
settings and the dismissed findings, but not the lake (the history, the findings and the
weekly summaries, tens of GB). After a restore xomtra starts a new lake and loads the
history again from Home Assistant, only as far back as Home Assistant still keeps it,
and computes the findings again.

For a robust setup with backups of the whole history, run xomtra on its own virtual
machine (Proxmox, VMware or Docker on Linux) and back up that machine.

## More

Documentation: https://github.com/xomtra/xomtra/blob/main/docs/setup/home-assistant-addon.md
