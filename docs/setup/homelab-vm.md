# Host VM (Proxmox, Debian 13)

## 1. Network

Create a dedicated VLAN for HAmmond on the router/firewall and allow only:

| From | To | Port | Why |
| --- | --- | --- | --- |
| HAmmond VLAN | Internet | 443/tcp | Entra ID, GHCR images, Claude, Tailscale |
| HAmmond VLAN | Home Assistant | 8123/tcp | WebSocket API |
| HAmmond VLAN | DNS/NTP servers | 53, 123 | |
| Admin network | HAmmond VM | 22/tcp | SSH |

Everything else between VLANs: deny. Inbound from the Internet stays closed in phase
1a.1/1a.2 (the PoC is reached through Tailscale Funnel, see the connector guide); the
port forward restricted to Anthropic's IP ranges comes in iteration 1a.3.

## 2. Create the VM in Proxmox

Debian 13 netinst ISO, then **Create VM**:

| Setting | Value |
| --- | --- |
| Machine / BIOS | q35, OVMF (UEFI), EFI disk, no pre-enrolled keys needed |
| CPU | 4 vCPU, type `host` |
| Memory | 8 GiB (no ballooning below 6 GiB) |
| Disk | 64 GiB, VirtIO SCSI single, `discard=on`, `ssd=1`, `iothread=1` |
| Network | VirtIO, bridge with **VLAN tag** of the HAmmond VLAN, Proxmox firewall on |
| Options | QEMU guest agent enabled, start at boot |

Install Debian: SSH server + standard system utilities only (no desktop), create your
admin user, put your SSH public key into `~/.ssh/authorized_keys`.
Include the VM in your Proxmox Backup Server job.

## 3. Bootstrap

```bash
scp deploy/vm/bootstrap-debian13.sh <admin>@<vm>:
ssh <admin>@<vm>
sudo ADMIN_CIDR=<admin-network-cidr> INSTALL_TAILSCALE=1 ./bootstrap-debian13.sh
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
sudo docker compose ps        # ducklake-init: exited (0), mcp: healthy
curl -fsS http://127.0.0.1:8000/healthz
```

Verify the image signature before the first deployment (see README, `cosign verify`) and
pin `HAMMOND_MCP_IMAGE` to the verified digest. The pull agent that does this
automatically comes in a later iteration.

Home Assistant (optional for 1a.1): create a dedicated **non-admin** HA user, a
long-lived access token for it, and set `HAMMOND_HA_URL` / `HAMMOND_HA_TOKEN`.
