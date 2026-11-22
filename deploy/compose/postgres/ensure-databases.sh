#!/bin/sh
# Idempotently creates the roles and databases of HAmmond (runs on every `up`, so that
# existing installations get databases added by newer versions). Passwords are kept in
# sync with .env.
set -eu
: "${PGHOST:?}" "${PGPASSWORD:?}"
: "${DUCKLAKE_DB:?}" "${DUCKLAKE_USER:?}" "${DUCKLAKE_PASSWORD:?}"
: "${OPS_USER:?}" "${OPS_PASSWORD:?}" "${DAGSTER_DB:?}" "${SQLMESH_DB:?}"

ensure() { # role password database...
  role=$1 pwd=$2
  shift 2
  psql -v ON_ERROR_STOP=1 -U postgres -d postgres -v usr="$role" -v pwd="$pwd" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'usr')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'usr') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'usr', :'pwd') \gexec
SQL
  for db in "$@"; do
    psql -v ON_ERROR_STOP=1 -U postgres -d postgres -v usr="$role" -v db="$db" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'usr')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
  done
}

ensure "$DUCKLAKE_USER" "$DUCKLAKE_PASSWORD" "$DUCKLAKE_DB"
ensure "$OPS_USER" "$OPS_PASSWORD" "$DAGSTER_DB" "$SQLMESH_DB"
echo "Postgres roles and databases are in place."
