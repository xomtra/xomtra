#!/bin/sh
# Starts the add-on image like the Supervisor would (a /data volume with options.json,
# SUPERVISOR_TOKEN, the hassio network) and waits until Postgres, the catalog, the MCP
# server and the admin GUI are up; the admin GUI is called like HA Ingress does it.
# Home Assistant is not reachable here; the worker keeps retrying, which is expected.
#   addon/xomtra/smoke-test.sh xomtra-addon:ci
set -eu
IMAGE=${1:?image}
DATA=$(mktemp -d)
NAME=xomtra-addon-smoke
NET=xomtra-smoke-hassio
cat >"$DATA/options.json" <<'JSON'
{"log_level": "info", "states_retention_days": 90, "statistics_5min_retention_days": 90,
 "statistics_hour_retention_days": 1825, "mcp_url": ""}
JSON
cleanup() {
  docker logs "$NAME" 2>&1 | tail -80
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Like the Supervisor's hassio network: Home Assistant's Ingress proxy is 172.30.32.2.
docker network create --subnet 172.30.32.0/23 "$NET" >/dev/null
docker run -d --name "$NAME" --network "$NET" --ip 172.30.33.5 -p 127.0.0.1:18000:8000 \
  -v "$DATA:/data" -e SUPERVISOR_TOKEN=smoke-test -e TZ=Europe/Prague "$IMAGE" >/dev/null

# GET on the admin GUI from address $1 with the headers of HA Ingress; prints the status.
ingress_get() {
  docker run --rm --network "$NET" --ip "$1" --entrypoint python3 "$IMAGE" -c '
import sys, urllib.error, urllib.request
req = urllib.request.Request("http://172.30.33.5:8100/tokens", headers={
    "X-Ingress-Path": "/api/hassio_ingress/smoke", "X-Forwarded-Host": "ha.local:8123",
    "X-Remote-User-Id": "u1", "X-Remote-User-Name": "smoke"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
        assert "/api/hassio_ingress/smoke/catalog" in body, body[:500]
        print(r.status)
except urllib.error.HTTPError as err:
    print(err.code)'
}

i=0
until curl -fsS http://127.0.0.1:18000/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then echo "MCP server did not become healthy" >&2; exit 1; fi
  sleep 2
done
echo "MCP server healthy after ~$((i * 2)) s"

# The worker runs and the secrets and the cluster are in /data, not in the image.
docker exec "$NAME" /command/s6-svstat /run/service/worker | grep -q '^up'
i=0
until docker exec "$NAME" python3 -c 'import urllib.request as u; u.urlopen("http://127.0.0.1:8100/healthz", timeout=5)' 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then echo "Admin GUI did not start" >&2; exit 1; fi
  sleep 2
done
# Only Home Assistant's Ingress proxy may open the admin GUI.
test "$(ingress_get 172.30.32.2)" = 200
test "$(ingress_get 172.30.32.9)" = 403
echo "Admin GUI answers HA Ingress only"
docker exec "$NAME" test -s /data/secrets.env
docker exec "$NAME" test -s /data/postgres/PG_VERSION
test "$(docker exec "$NAME" stat -c %a /data/secrets.env)" = 600

# A restart keeps the generated secrets (the catalog stays readable).
before=$(docker exec "$NAME" md5sum /data/secrets.env)
docker restart "$NAME" >/dev/null
i=0
until curl -fsS http://127.0.0.1:18000/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then echo "MCP server did not come back after a restart" >&2; exit 1; fi
  sleep 2
done
test "$before" = "$(docker exec "$NAME" md5sum /data/secrets.env)"

# Memory at rest (target <= 2 GiB, plan 1c.2 PR 9): every service has started and
# answered once; the lake is empty here, so this is the floor, not a measurement with
# a real installation.
until docker exec "$NAME" python3 -c 'import urllib.request as u; u.urlopen("http://127.0.0.1:8100/healthz", timeout=5)' 2>/dev/null; do sleep 2; done
test "$(ingress_get 172.30.32.2)" = 200
sleep "${SETTLE_SECONDS:-60}"
docker exec "$NAME" python3 -c '
import os
rows = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()[:70]
        rss = next(int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS"))
    except (OSError, StopIteration):
        continue
    rows.append((rss, cmd))
for rss, cmd in sorted(rows, reverse=True)[:12]:
    print(f"{rss / 1024:8.1f} MiB  {cmd}")'
used=$(docker stats --no-stream --format '{{.MemUsage}}' "$NAME" | cut -d/ -f1 | tr -d ' ')
bytes=$(python3 -c '
import re, sys
n, unit = re.fullmatch(r"([0-9.]+)([KMG]i?B|B)", sys.argv[1]).groups()
scale = {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "kB": 1e3, "MB": 1e6, "GB": 1e9}
print(int(float(n) * scale[unit]))' "$used")
echo "Memory at rest: $used (limit 2 GiB)"
test "$bytes" -le $((2 * 1024 * 1024 * 1024)) || { echo "Memory at rest above 2 GiB" >&2; exit 1; }
echo "Add-on smoke test passed"
