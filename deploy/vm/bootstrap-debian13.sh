#!/usr/bin/env bash
# Bootstrap a fresh Debian 13 (trixie) VM as the xomtra host.
#
#   sudo ADMIN_CIDR=192.0.2.0/24 ./bootstrap-debian13.sh
#
# Optional:  ADMIN_USER=<name>     existing user that keeps SSH access (default: $SUDO_USER)
#
# Idempotent: safe to run again. Installs Docker Engine + Compose from Docker's apt
# repository, hardens SSH, enables a default-deny firewall and unattended upgrades.
set -euo pipefail

: "${ADMIN_CIDR:?Set ADMIN_CIDR to the network allowed to SSH in, e.g. ADMIN_CIDR=192.0.2.0/24}"
ADMIN_USER="${ADMIN_USER:-${SUDO_USER:-}}"
XOMTRA_DIR=/opt/xomtra

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
. /etc/os-release
[[ "${VERSION_CODENAME:-}" == "trixie" ]] || { echo "Expected Debian 13 (trixie), got ${PRETTY_NAME:-unknown}." >&2; exit 1; }
if [[ -z "$ADMIN_USER" ]] || ! id "$ADMIN_USER" >/dev/null 2>&1; then
  echo "ADMIN_USER is unset or does not exist; refusing to lock down SSH." >&2; exit 1
fi
if [[ ! -s "$(getent passwd "$ADMIN_USER" | cut -d: -f6)/.ssh/authorized_keys" ]]; then
  echo "$ADMIN_USER has no ~/.ssh/authorized_keys; add your key first (password login gets disabled)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
echo "==> Base packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq --no-install-recommends \
  ca-certificates curl gnupg jq ufw unattended-upgrades apt-listchanges qemu-guest-agent \
  chrony
systemctl enable --now qemu-guest-agent chrony || true

echo "==> Unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

echo "==> Docker Engine (docker.com apt repository)"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<SRC
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
SRC
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true,
  "no-new-privileges": true,
  "userland-proxy": false
}
JSON
systemctl enable docker
systemctl restart docker

echo "==> SSH hardening"
cat > /etc/ssh/sshd_config.d/10-xomtra.conf <<CONF
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
AllowUsers ${ADMIN_USER}
CONF
sshd -t
systemctl reload ssh

echo "==> Firewall (default deny inbound, SSH only from ${ADMIN_CIDR})"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp comment 'SSH from admin network'
ufw --force enable
# Note: ports published by Docker bypass ufw. The compose stack publishes only Caddy
# (443); the router's port forward and Caddy's IP rules control who reaches it.

echo "==> Application directory ${XOMTRA_DIR}"
install -d -m 0750 -o root -g root "$XOMTRA_DIR"

echo
echo "Done. Next steps (docs/setup/homelab-vm.md):"
echo "  1. Copy deploy/compose/* to ${XOMTRA_DIR}, create ${XOMTRA_DIR}/.env (chmod 600)."
echo "  2. docker compose -f ${XOMTRA_DIR}/compose.yaml up -d"
