#!/bin/sh
# Optional guide that writes .env for the compose stack (ADR 077). It is not needed for a
# normal install: `docker compose up -d` starts with defaults, generates the internal
# passwords and keys into the volume "secrets", and Home Assistant is connected in the
# admin GUI. Use this for a non-interactive install (Home Assistant URL and token given
# up front, the setup page is then skipped) or for the public access mode.
#
#   ./init-env.sh            interactive; answers can also come from the environment:
#   XOMTRA_ACCESS_MODE=local XOMTRA_LOCAL_HOST=xomtra.home.arpa \
#   XOMTRA_HA_URL=http://homeassistant.lan:8123 XOMTRA_HA_TOKEN=... ./init-env.sh
#
#   ./init-env.sh --fill     keep an existing .env, only add keys of the current template.
#
# Internal passwords are no longer written here. An older .env that still has them is
# taken over into the secrets volume on the first start of the new version; afterwards
# they can be deleted from .env. .env is created with mode 600 (it may hold the HA token).
set -eu
cd "$(dirname "$0")"

ENV_FILE=${ENV_FILE:-.env}
TEMPLATE=.env.example

set_value() { # KEY VALUE: replace the line or append it; values are taken literally
  tmp="$ENV_FILE.tmp.$$"
  K=$1 V=$2 awk '
    BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" >"$tmp"
  cat "$tmp" >"$ENV_FILE"
  rm -f "$tmp"
}

ask() { # VAR "question" default: environment > answer > default
  eval "current=\${$1:-}"
  if [ -n "$current" ]; then
    answer=$current
  elif [ -t 0 ]; then
    printf '%s [%s]: ' "$2" "$3" >&2
    read -r answer || answer=""
    answer=${answer:-$3}
  else
    answer=$3
  fi
  eval "$1=\$answer"
}

umask 077
if [ "${1:-}" = "--fill" ]; then
  [ -f "$ENV_FILE" ] || { echo "$ENV_FILE does not exist; run without --fill" >&2; exit 1; }
  grep -E '^[A-Z_][A-Z0-9_]*=' "$TEMPLATE" | while IFS= read -r line; do
    key=${line%%=*}
    grep -q "^$key=" "$ENV_FILE" || { printf '%s\n' "$line" >>"$ENV_FILE"; echo "added $key" >&2; }
  done
  exit 0
fi

if [ -f "$ENV_FILE" ]; then
  echo "$ENV_FILE already exists; use --fill to complete it, or move it away first" >&2
  exit 1
fi
cp "$TEMPLATE" "$ENV_FILE"
chmod 600 "$ENV_FILE"

ask XOMTRA_ACCESS_MODE "Access mode: local (LAN only) or public (Claude connector)" local
case $XOMTRA_ACCESS_MODE in local | public) ;; *) echo "local or public" >&2; exit 1 ;; esac
set_value XOMTRA_ACCESS_MODE "$XOMTRA_ACCESS_MODE"

if [ "$XOMTRA_ACCESS_MODE" = local ]; then
  ask XOMTRA_LOCAL_HOST "Name or IP address of this server in the LAN (empty = found at start)" ""
  set_value XOMTRA_LOCAL_HOST "$XOMTRA_LOCAL_HOST"
else
  ask XOMTRA_PUBLIC_HOST "Public DNS name of this server" mcp.example.org
  set_value XOMTRA_PUBLIC_HOST "$XOMTRA_PUBLIC_HOST"
  echo "Public access also needs ACME_EMAIL, CLOUDFLARE_API_TOKEN and CLAUDE_IP_RANGES" \
    "in $ENV_FILE (docs/setup/claude-connector.md)." >&2
fi

ask XOMTRA_HA_URL "Home Assistant URL as this server reaches it (empty = enter it in the admin GUI)" ""
if [ -n "$XOMTRA_HA_URL" ]; then
  set_value XOMTRA_HA_URL "$XOMTRA_HA_URL"
  ask XOMTRA_HA_TOKEN "Long-lived token of the xomtra user in HA (docs/setup/ingest.md)" ""
  [ -n "$XOMTRA_HA_TOKEN" ] && set_value XOMTRA_HA_TOKEN "$XOMTRA_HA_TOKEN"
fi

echo "Wrote $ENV_FILE. Next: docker compose up -d" >&2
if [ -z "$XOMTRA_HA_URL" ]; then
  echo "Then open the setup link from: docker compose logs admin" >&2
fi
