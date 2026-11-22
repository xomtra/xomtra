# Host VM (Proxmox, Debian 13)

## 1. Network

Put the VM into a DMZ VLAN (`<dmz-cidr>`) whose default policy towards the internal
networks is **deny**. The VM is the only Internet-facing service of HAmmond, so a
compromise must not reach the rest of the home network. Rules:

| From | To | Port | Why |
| --- | --- | --- | --- |
| Internet | VM (port forward) | 443/tcp | Caddy: Claude's servers + your browser during sign-in |
| DMZ (VM only) | Home Assistant | 8123/tcp | WebSocket API |
| DMZ | Internet | 443/tcp | Entra ID, Let's Encrypt, Cloudflare API, GHCR |
| DMZ | DNS/NTP servers | 53, 123 | |
| Admin network | VM | 22/tcp | SSH |

Everything else from the DMZ to internal VLANs: deny. Do not forward port 80 (certificates
use the DNS-01 challenge). The port forward has to accept any source address because
the OAuth sign-in steps run in your browser; Caddy restricts everything else to Claude's
IP ranges (see the connector guide). Enable IPS/geo filtering on the router if you have
it.

## 2. Create the VM in Proxmox

Debian 13 netinst ISO, then **Create VM**:

| Setting | Value |
| --- | --- |
| Machine / BIOS | q35, OVMF (UEFI), EFI disk, no pre-enrolled keys needed |
| CPU | 4 vCPU, type `host` |
| Memory | 8 GiB (no ballooning below 6 GiB) |
| Disk | 64 GiB, VirtIO SCSI single, `discard=on`, `ssd=1`, `iothread=1` |
| Network | VirtIO, bridge with **VLAN tag** of the DMZ VLAN, Proxmox firewall on |
| Options | QEMU guest agent enabled, start at boot |

Install Debian: SSH server + standard system utilities only (no desktop), create your
admin user, put your SSH public key into `~/.ssh/authorized_keys`.
Include the VM in your Proxmox Backup Server job.

## 3. Bootstrap

```bash
scp deploy/vm/bootstrap-debian13.sh <admin>@<vm>:
ssh <admin>@<vm>
sudo ADMIN_CIDR=<admin-network-cidr> ./bootstrap-debian13.sh
```

The script installs Docker Engine + Compose, hardens SSH (keys only, no root), enables
ufw (inbound SSH from the admin network only) and unattended security upgrades.

## 4. Deploy the stack

```bash
sudo install -d -m 750 /opt/hammond
sudo cp -r deploy/compose/* /opt/hammond/
sudo cp deploy/compose/.env.example /opt/hammond/.env
sudo chmod 600 /opt/hammond/.env && sudoedit /opt/hammond/.env   # fill in all values
cd /opt/hammond && sudo docker compose pull && sudo docker compose up -d
sudo docker compose ps        # ducklake-init: exited (0), mcp: healthy, caddy: up
sudo docker compose logs caddy | grep -i certificate   # certificate obtained
```

Verify the image signature before the first deployment (see README, `cosign verify`) and
pin `HAMMOND_MCP_IMAGE` to the verified digest. The pull agent that does this
automatically comes in a later iteration.

Home Assistant (optional for 1a.1): create a dedicated **non-admin** HA user, a
long-lived access token for it, and set `HAMMOND_HA_URL` / `HAMMOND_HA_TOKEN`.
