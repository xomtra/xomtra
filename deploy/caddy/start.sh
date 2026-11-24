#!/bin/sh
# Picks the Caddy configuration of the access mode (ADR 056) and checks its settings,
# so that a missing value fails here with a clear message instead of inside Caddy.
#   HAMMOND_ACCESS_MODE=public (default): public host, ACME DNS-01, Claude IP filter.
#   HAMMOND_ACCESS_MODE=local: LAN only, Caddy's local CA, no internet exposure.
set -eu
mode=${HAMMOND_ACCESS_MODE:-public}
need() {
  for name in "$@"; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
      echo "HAMMOND_ACCESS_MODE=$mode requires $name (set it in .env)" >&2
      exit 1
    fi
  done
}
case $mode in
  public)
    need HAMMOND_PUBLIC_HOST ACME_EMAIL CLOUDFLARE_API_TOKEN CLAUDE_IP_RANGES ADMIN_CIDR
    config=/etc/caddy/Caddyfile ;;
  local)
    need HAMMOND_LOCAL_HOST LAN_CIDR ADMIN_CIDR
    config=/etc/caddy/Caddyfile.local ;;
  *)
    echo "HAMMOND_ACCESS_MODE must be public or local, not '$mode'" >&2
    exit 1 ;;
esac
exec caddy run --config "$config" --adapter caddyfile
