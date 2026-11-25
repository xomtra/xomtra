#!/bin/sh
# Creates .env for the compose stack (ADR 061): every password and key between the
# internal components (Postgres roles, SeaweedFS/S3, MCP OAuth state, admin sessions) is
# generated here, so nobody has to invent or copy them. It only asks for what it cannot
# know: the access mode, the host name and the Home Assistant connection.
#
#   ./init-env.sh            interactive; answers can also come from the environment:
#   XOMTRA_ACCESS_MODE=local XOMTRA_LOCAL_HOST=xomtra.lan ADMIN_CIDR=192.168.1.0/24 \
#   XOMTRA_HA_URL=http://homeassistant.lan:8123 XOMTRA_HA_TOKEN=... ./init-env.sh
#
#   ./init-env.sh --fill     keep an existing .env, only add missing keys of the current
#                            template and generate internal secrets that are empty or
#                            still "change-me" (after an update). Never changes others.
#
# The secrets never leave this machine; .env is created with mode 600.
set -eu
cd "$(dirname "$0")"

ENV_FILE=${ENV_FILE:-.env}
TEMPLATE=.env.example
INTERNAL_SECRETS="POSTGRES_PASSWORD DUCKLAKE_PG_PASSWORD OPS_PG_PASSWORD AUDIT_PG_PASSWORD
AUDIT_ADMIN_PG_PASSWORD S3_ACCESS_KEY S3_SECRET_KEY XOMTRA_JWT_SIGNING_KEY
XOMTRA_ADMIN_SESSION_KEY"

secret() { # 40 random characters [A-Za-z0-9]: safe in .env, URLs and libpq strings
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40
}

get() { # value of KEY in .env (empty if missing)
  K=$1 awk 'index($0, ENVIRON["K"] "=") == 1 { print substr($0, length(ENVIRON["K"]) + 2); exit }' \
    "$ENV_FILE"
}

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

fill_secrets() {
  for key in $INTERNAL_SECRETS; do
    value=$(get "$key")
    if [ -z "$value" ] || [ "$value" = "change-me" ]; then
      set_value "$key" "$(secret)"
      echo "generated $key" >&2
    fi
  done
}

umask 077
if [ "${1:-}" = "--fill" ]; then
  [ -f "$ENV_FILE" ] || { echo "$ENV_FILE does not exist; run without --fill" >&2; exit 1; }
  # Keys added to the template by newer versions, with their template defaults.
  grep -E '^[A-Z_][A-Z0-9_]*=' "$TEMPLATE" | while IFS= read -r line; do
    key=${line%%=*}
    grep -q "^$key=" "$ENV_FILE" || { printf '%s\n' "$line" >>"$ENV_FILE"; echo "added $key" >&2; }
  done
  fill_secrets
  exit 0
fi

if [ -f "$ENV_FILE" ]; then
  echo "$ENV_FILE already exists; use --fill to complete it, or move it away first" >&2
  exit 1
fi
cp "$TEMPLATE" "$ENV_FILE"
chmod 600 "$ENV_FILE"
fill_secrets

guess_ip=$(hostname -I 2>/dev/null | awk '{ print $1 }')
ask XOMTRA_ACCESS_MODE "Access mode: local (LAN only) or public (Claude connector)" local
case $XOMTRA_ACCESS_MODE in local | public) ;; *) echo "local or public" >&2; exit 1 ;; esac
set_value XOMTRA_ACCESS_MODE "$XOMTRA_ACCESS_MODE"

if [ "$XOMTRA_ACCESS_MODE" = local ]; then
  ask XOMTRA_LOCAL_HOST "Name or IP address of this server in the LAN" "${guess_ip:-xomtra.lan}"
  set_value XOMTRA_LOCAL_HOST "$XOMTRA_LOCAL_HOST"
  set_value XOMTRA_BASE_URL "https://$XOMTRA_LOCAL_HOST"
  default_auth=ha
else
  ask XOMTRA_PUBLIC_HOST "Public DNS name of this server" mcp.example.org
  set_value XOMTRA_PUBLIC_HOST "$XOMTRA_PUBLIC_HOST"
  set_value XOMTRA_BASE_URL "https://$XOMTRA_PUBLIC_HOST"
  echo "Public access also needs ACME_EMAIL, CLOUDFLARE_API_TOKEN and CLAUDE_IP_RANGES" \
    "in $ENV_FILE (docs/setup/claude-connector.md)." >&2
  default_auth=ha
fi

ask XOMTRA_AUTH_MODE "Sign-in: ha (Home Assistant account) or entra" "$default_auth"
set_value XOMTRA_AUTH_MODE "$XOMTRA_AUTH_MODE"

default_admin=""
case $guess_ip in
  *.*.*.*) default_admin="${guess_ip%.*}.0/24" ;;
esac
ask ADMIN_CIDR "Network allowed to open the admin GUI (CIDR)" "${default_admin:-192.168.0.0/16}"
set_value ADMIN_CIDR "$ADMIN_CIDR"

ask XOMTRA_HA_URL "Home Assistant URL as this server reaches it" http://homeassistant.local:8123
set_value XOMTRA_HA_URL "$XOMTRA_HA_URL"
ask XOMTRA_HA_TOKEN "Long-lived token of the Xomtra service user in HA (docs/setup/ingest.md)" ""
[ -n "$XOMTRA_HA_TOKEN" ] && set_value XOMTRA_HA_TOKEN "$XOMTRA_HA_TOKEN"

echo "Wrote $ENV_FILE. Next: docker compose up -d" >&2
if [ "$XOMTRA_ACCESS_MODE" = local ]; then
  echo "Then open https://$XOMTRA_LOCAL_HOST/admin/ and issue a token (Tokens)." >&2
fi
