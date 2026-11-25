#!/bin/sh
# Starts the add-on image like the Supervisor would (a /data volume with options.json,
# SUPERVISOR_TOKEN) and waits until Postgres, the catalog and the MCP server are up.
# Home Assistant is not reachable here; the worker keeps retrying, which is expected.
#   addon/hammond/smoke-test.sh hammond-addon:ci
set -eu
IMAGE=${1:?image}
DATA=$(mktemp -d)
NAME=hammond-addon-smoke
cat >"$DATA/options.json" <<'JSON'
{"log_level": "info", "states_retention_days": 90, "statistics_5min_retention_days": 90,
 "statistics_hour_retention_days": 1825, "mcp_url": ""}
JSON
cleanup() { docker logs "$NAME" 2>&1 | tail -80; docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$NAME" -p 127.0.0.1:18000:8000 -v "$DATA:/data" \
  -e SUPERVISOR_TOKEN=smoke-test -e TZ=Europe/Prague "$IMAGE" >/dev/null

i=0
until curl -fsS http://127.0.0.1:18000/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then echo "MCP server did not become healthy" >&2; exit 1; fi
  sleep 2
done
echo "MCP server healthy after ~$((i * 2)) s"

# The worker runs and the secrets and the cluster are in /data, not in the image.
docker exec "$NAME" /command/s6-svstat /run/service/worker | grep -q '^up'
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
echo "Add-on smoke test passed"
