# Host VM (Proxmox, Debian 13)

The VM is the only Internet-facing part of HAmmond. It goes into an **existing DMZ
network** (`<dmz-cidr>`) that is isolated from the internal networks by the router; no
new VLAN is needed. Placeholders: `<vm-ip>` (static address of the VM in the DMZ),
`<ha-ip>` (Home Assistant), `<admin-cidr>` (network you administer from).

## 1. Router / firewall rules

Assumed baseline of the DMZ: traffic DMZ → internal is blocked by default, only return
traffic of connections opened from internal networks is allowed. Add or check:

| # | From | To | Protocol / port | Purpose |
| --- | --- | --- | --- | --- |
| 1 | DMZ `<vm-ip>` | internal `<ha-ip>` | TCP 8123 | Home Assistant WebSocket API |
| 2 | admin network `<admin-cidr>` | DMZ `<vm-ip>` | TCP 22 | SSH |
| 3 | WAN | `<vm-ip>` | TCP 443 (port forward) | Caddy: Claude's servers and your browser during sign-in |
| 4 | DMZ `<vm-ip>` | Internet | TCP 443, UDP/TCP 53, UDP 123 | Entra ID, Let's Encrypt, Cloudflare API, GHCR, DNS, NTP |

Rule 1 must sit above the DMZ → internal block rule. Do **not** create a reverse
("return") rule for the SSH rule: answers to your SSH session are already covered by the
return-traffic rule, and a reverse rule would let the VM open connections into the
internal network on its own.

Port forward: only TCP 443, no port 80 (certificates use the DNS-01 challenge). It has
to accept any source address because the OAuth sign-in steps run in your browser; Caddy
restricts everything else to Claude's IP ranges (see the [connector guide](claude-connector.md)).
Enable IPS / geo filtering for the forward if your router supports it.

## 2. Create the VM in Proxmox

Debian 13 netinst ISO, then **Create VM**:

| Setting | Value |
| --- | --- |
| Machine / BIOS | q35, OVMF (UEFI), EFI disk |
| CPU | 4 vCPU, type `host` |
| Memory | 8 GiB |
| Disk | 64 GiB, VirtIO SCSI single, `discard=on`, `ssd=1`, `iothread=1` |
| Network | VirtIO on the bridge / VLAN tag of the **existing DMZ**, Proxmox firewall on |
| Options | QEMU guest agent enabled, start at boot |

Debian installer: static address `<vm-ip>` in the DMZ (or a DHCP reservation), only
*SSH server* and *standard system utilities* (no desktop), your admin user. Then put
your SSH public key into `~/.ssh/authorized_keys` of that user.
Add the VM to your Proxmox Backup Server job.

## 3. Bootstrap

```bash
ssh <admin>@<vm-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/xomtra/hammond.git ~/hammond
sudo ADMIN_CIDR=<admin-cidr> ~/hammond/deploy/vm/bootstrap-debian13.sh
```

`<admin-cidr>` is the network **of the machine you are typing on** (check its address),
not the network of Home Assistant. A wrong value locks you out of SSH as soon as ufw is
enabled; your current session survives. Fix it from the Proxmox shell through the guest
agent the script installs:

```bash
qm guest exec <vmid> -- ufw allow from <admin-cidr> to any port 22 proto tcp
```

The script installs Docker Engine + Compose, hardens SSH (keys only, no root, only your
user), enables ufw (inbound SSH from `<admin-cidr>` only) and unattended security
upgrades. Ports published by Docker (443 of Caddy) bypass ufw by design; the router
rules above decide who reaches them.

Note: when you change the VM's cloud-init settings (`qm set --ciuser/--sshkeys/...`),
cloud-init treats the next boot as a new instance and generates new SSH host keys.
Compare the new fingerprint (`qm guest exec <vmid> -- ssh-keygen -lf
/etc/ssh/ssh_host_ed25519_key.pub`) and then `ssh-keygen -R <vm-ip>` on your machine.

## 4. Deploy the stack

Prerequisites: [Entra app registration](entra-app-registration.md), DNS record and
Cloudflare token from the [connector guide](claude-connector.md), and the GHCR packages
`hammond-mcp` and `hammond-caddy` set to *Public* (otherwise `docker login ghcr.io`
with a read-only token first).

`/opt/hammond` is readable by root only (it holds `.env`), so work in a root shell:

```bash
sudo -i
install -d -m 750 /opt/hammond
cp -r ~<admin>/hammond/deploy/compose/. /opt/hammond/
cp /opt/hammond/.env.example /opt/hammond/.env
chmod 600 /opt/hammond/.env && editor /opt/hammond/.env   # fill in all values
cd /opt/hammond
docker compose pull
docker compose up -d
docker compose ps                     # ducklake-init exited (0), mcp healthy, caddy up
docker compose logs caddy | grep -i "certificate obtained"
exit
```

Before the first deployment take the `*_IMAGE` lines from the release asset
`hammond-<version>-images.env` and verify the signatures ([Releases](../releasing.md)).
Automatic pull + verification comes in a later iteration.

Home Assistant: create a dedicated **non-admin** HA user, a long-lived access token
for it, and set `HAMMOND_HA_URL=http://<ha-ip>:8123` and `HAMMOND_HA_TOKEN` in `.env`.
The ingest (phase 1a.2) needs it; see [Data ingest](ingest.md).
