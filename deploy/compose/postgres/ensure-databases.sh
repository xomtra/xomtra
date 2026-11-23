#!/bin/sh
# Idempotently creates the roles and databases of HAmmond (runs on every `up`, so that
# existing installations get databases added by newer versions). Passwords are kept in
# sync with .env.
set -eu
: "${PGHOST:?}" "${PGPASSWORD:?}"
: "${DUCKLAKE_DB:?}" "${DUCKLAKE_USER:?}" "${DUCKLAKE_PASSWORD:?}"
: "${OPS_USER:?}" "${OPS_PASSWORD:?}" "${DAGSTER_DB:?}" "${SQLMESH_DB:?}"
: "${AUDIT_DB:?}" "${AUDIT_USER:?}" "${AUDIT_PASSWORD:?}"
: "${AUDIT_ADMIN_USER:?}" "${AUDIT_ADMIN_PASSWORD:?}"

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

# Audit of MCP tool calls (ADR 040): the database and table belong to postgres; the MCP
# server's role may only connect and INSERT, so it can neither read nor change history.
ensure "$AUDIT_USER" "$AUDIT_PASSWORD"
# The admin GUI reads the audit and records its own page views (SELECT + INSERT).
ensure "$AUDIT_ADMIN_USER" "$AUDIT_ADMIN_PASSWORD"
psql -v ON_ERROR_STOP=1 -U postgres -d postgres -v db="$AUDIT_DB" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER postgres', :'db')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
psql -v ON_ERROR_STOP=1 -U postgres -d "$AUDIT_DB" -v usr="$AUDIT_USER" -v db="$AUDIT_DB" \
  -v adm="$AUDIT_ADMIN_USER" <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS audit.tool_call (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts          timestamptz NOT NULL,
  subject     text,
  username    text,
  roles       text[] NOT NULL DEFAULT '{}',
  client_id   text,
  tool        text NOT NULL,
  arguments   jsonb NOT NULL DEFAULT '{}',
  decision    text NOT NULL CHECK (decision IN ('allowed', 'denied')),
  outcome     text NOT NULL CHECK (outcome IN ('ok', 'denied', 'error')),
  error       text,
  row_count   integer,
  duration_ms integer
);
CREATE INDEX IF NOT EXISTS tool_call_ts ON audit.tool_call (ts);
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'usr') \gexec
SELECT format('GRANT USAGE ON SCHEMA audit TO %I', :'usr') \gexec
SELECT format('GRANT INSERT ON audit.tool_call TO %I', :'usr') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'adm') \gexec
SELECT format('GRANT USAGE ON SCHEMA audit TO %I', :'adm') \gexec
SELECT format('GRANT SELECT, INSERT ON audit.tool_call TO %I', :'adm') \gexec
SQL
echo "Postgres roles and databases are in place."
