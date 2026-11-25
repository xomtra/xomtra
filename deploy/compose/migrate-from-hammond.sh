#!/bin/sh
# One-off migration of a compose installation from the code name "hammond" to "xomtra"
# (ADR 071): environment variables, compose project (containers and volumes), Postgres
# roles, the lake bucket and its path in the DuckLake catalog. Run it in the directory
# with compose.yaml and .env, AFTER the *_IMAGE lines of .env point to xomtra images of
# a release that already uses the new names:
#
#   sh migrate-from-hammond.sh            # stops the old stack, migrates, starts the new
#
# Nothing old is deleted: the old volumes (hammond_*), the old bucket and .env.bak-hammond
# stay until you remove them yourself (the commands are printed at the end).
set -eu

OLD=hammond
NEW=xomtra
VOLUMES="caddy-data caddy-config postgres-data seaweedfs-data mcp-data ingest-data"
AWS_CLI=amazon/aws-cli:2.15.0

die() { echo "error: $*" >&2; exit 1; }
[ -f compose.yaml ] && [ -f .env ] || die "run this next to compose.yaml and .env"
grep -q '^HAMMOND_' .env || die ".env has no HAMMOND_* variables (already migrated?)"
grep -Eq '^HAMMOND_(MCP|INGEST|CADDY)_IMAGE=.*/xomtra-' .env \
  || die "first set the *_IMAGE lines to the xomtra images of the new release"

envval() { sed -n "s/^$1=//p" .env | tail -n 1; }
old_bucket=$(envval S3_BUCKET); old_bucket=${old_bucket:-$OLD}

echo "== stopping the old stack ($OLD)"
docker compose -p "$OLD" down

echo "== rewriting .env (backup .env.bak-$OLD)"
cp -p .env ".env.bak-$OLD"
sed -i \
  -e 's/^HAMMOND_/XOMTRA_/' \
  -e "s/^COMPOSE_PROJECT_NAME=.*/COMPOSE_PROJECT_NAME=$NEW/" \
  -e 's/=hammond_ops$/=xomtra_ops/' \
  -e 's/=hammond_audit$/=xomtra_audit/' \
  -e 's/=hammond_audit_admin$/=xomtra_audit_admin/' \
  -e 's/HAmmond\.\(Admin\|Reader\)/xomtra.\1/g' \
  -e 's/hammond_no_\(ingest\|analysis\)/xomtra_no_\1/g' \
  -e "s/^S3_BUCKET=$old_bucket\$/S3_BUCKET=$NEW/" \
  .env
grep -q '^COMPOSE_PROJECT_NAME=' .env || echo "COMPOSE_PROJECT_NAME=$NEW" >> .env
if [ "$old_bucket" = "$OLD" ] && ! grep -q '^S3_BUCKET=' .env; then
  echo "S3_BUCKET=$NEW" >> .env
fi
new_bucket=$(envval S3_BUCKET)

echo "== copying volumes ${OLD}_* -> ${NEW}_*"
helper=$(docker compose config --images | grep -m1 '^postgres') || die "no postgres image"
for v in $VOLUMES; do
  docker volume inspect "${OLD}_$v" >/dev/null 2>&1 || { echo "   ${OLD}_$v: none"; continue; }
  if docker volume inspect "${NEW}_$v" >/dev/null 2>&1; then
    die "${NEW}_$v already exists; remove it or finish the migration by hand"
  fi
  docker volume create "${NEW}_$v" >/dev/null
  docker run --rm --network none --entrypoint sh -v "${OLD}_$v:/from:ro" -v "${NEW}_$v:/to" \
    "$helper" -c 'cp -a /from/. /to/'
  echo "   ${OLD}_$v -> ${NEW}_$v"
done

echo "== renaming the Postgres roles"
docker compose up -d --wait postgres
pg_user=postgres
for role in ops audit audit_admin; do
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d postgres -c "
    DO \$\$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = '${OLD}_$role')
         AND NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${NEW}_$role') THEN
        ALTER ROLE ${OLD}_$role RENAME TO ${NEW}_$role;
      END IF;
    END \$\$;" >/dev/null
  echo "   ${OLD}_$role -> ${NEW}_$role"
done

if [ "$old_bucket" != "$new_bucket" ]; then
  echo "== copying the lake bucket $old_bucket -> $new_bucket"
  docker compose run --rm seaweedfs-init
  docker run --rm --network "${NEW}_backend" \
    -e AWS_ACCESS_KEY_ID="$(envval S3_ACCESS_KEY)" -e AWS_SECRET_ACCESS_KEY="$(envval S3_SECRET_KEY)" \
    -e AWS_DEFAULT_REGION=us-east-1 "$AWS_CLI" \
    --endpoint-url http://seaweedfs:8333 s3 sync --only-show-errors \
    "s3://$old_bucket" "s3://$new_bucket"
  echo "== pointing the DuckLake catalog to s3://$new_bucket"
  lake_db=$(envval DUCKLAKE_PG_DB); lake_db=${lake_db:-ducklake}
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d "$lake_db" -c "
    BEGIN;
    UPDATE ducklake_metadata
       SET value = replace(value, 's3://$old_bucket/', 's3://$new_bucket/')
     WHERE key = 'data_path';
    UPDATE ducklake_data_file
       SET path = replace(path, 's3://$old_bucket/', 's3://$new_bucket/')
     WHERE NOT path_is_relative;
    UPDATE ducklake_delete_file
       SET path = replace(path, 's3://$old_bucket/', 's3://$new_bucket/')
     WHERE NOT path_is_relative;
    COMMIT;"
fi

echo "== starting the new stack ($NEW)"
docker compose up -d
docker compose ps

left=$(grep -ci hammond .env || true)
[ "$left" = 0 ] || { echo "note: .env still mentions hammond (hostnames are left alone):"; grep -in hammond .env | sed 's/=.*/=…/'; }
cat <<DONE

Migration done. Check the admin GUI and a query in Claude, then remove the old data:
  docker volume rm $(for v in $VOLUMES; do printf '%s_%s ' "$OLD" "$v"; done)
  # and the old bucket, with the same aws-cli call as above: s3 rb --force s3://$old_bucket
  rm .env.bak-$OLD
Also: roles xomtra.Admin / xomtra.Reader in Entra, labels xomtra_no_ingest /
xomtra_no_analysis in Home Assistant, and new personal tokens (xmt_...).
DONE
