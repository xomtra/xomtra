#!/bin/sh
# Idempotently creates the roles and databases of Xomtra (runs on every `up`, so that
# existing installations get databases added by newer versions). Passwords are kept in
# sync with .env.
set -eu
: "${PGHOST:?}" "${PGPASSWORD:?}"
: "${DUCKLAKE_DB:?}" "${DUCKLAKE_USER:?}" "${DUCKLAKE_PASSWORD:?}"
: "${OPS_USER:?}" "${OPS_PASSWORD:?}" "${DAGSTER_DB:?}" "${SQLMESH_DB:?}"
: "${AUDIT_DB:?}" "${AUDIT_USER:?}" "${AUDIT_PASSWORD:?}"
: "${AUDIT_ADMIN_USER:?}" "${AUDIT_ADMIN_PASSWORD:?}"
AUDIT_RETENTION_DAYS=${AUDIT_RETENTION_DAYS:-90}
case $AUDIT_RETENTION_DAYS in
  '' | *[!0-9]*) echo "AUDIT_RETENTION_DAYS must be a whole number of days" >&2; exit 1 ;;
esac

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
# OPS_DB: runs and cursors of the built-in worker (lite edition, ADR 050).
ensure "$OPS_USER" "$OPS_PASSWORD" "$DAGSTER_DB" "$SQLMESH_DB" "${OPS_DB:-ops}"

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
  -v adm="$AUDIT_ADMIN_USER" -v ops="$OPS_USER" -v days="$AUDIT_RETENTION_DAYS" <<'SQL'
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
-- Identity provider of the caller: entra | ha | token (ADR 053, 059); NULL before 1c.1.
ALTER TABLE audit.tool_call ADD COLUMN IF NOT EXISTS idp text;
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'usr') \gexec
SELECT format('GRANT USAGE ON SCHEMA audit TO %I', :'usr') \gexec
SELECT format('GRANT INSERT ON audit.tool_call TO %I', :'usr') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'adm') \gexec
SELECT format('GRANT USAGE ON SCHEMA audit TO %I', :'adm') \gexec
SELECT format('GRANT SELECT, INSERT ON audit.tool_call TO %I', :'adm') \gexec
-- Retention (ADR 040): only records older than AUDIT_RETENTION_DAYS (0 = keep all) can be
-- deleted, by a function owned by postgres. The MCP server calls it once a day; its role
-- still cannot delete or read anything else, so recent history cannot be erased.
SELECT format($f$
CREATE OR REPLACE FUNCTION audit.purge_expired() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $b$
  WITH gone AS (
    DELETE FROM audit.tool_call
    WHERE %1$s > 0 AND ts < now() - make_interval(days => %1$s)
    RETURNING 1)
  SELECT count(*) FROM gone
$b$$f$, :'days'::integer) \gexec
REVOKE ALL ON FUNCTION audit.purge_expired() FROM PUBLIC;
SELECT format('GRANT EXECUTE ON FUNCTION audit.purge_expired() TO %I, %I', :'usr', :'adm') \gexec

-- Personal access tokens of the local access mode (ADR 056, 059). Only SHA-256 hashes
-- are stored. The admin GUI lists, issues and revokes (no DELETE: history stays); the
-- MCP server may only call access.verify_token(), which returns a valid token's row and
-- records its last use, so it can neither list nor change tokens.
CREATE SCHEMA IF NOT EXISTS access AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS access.token (
  id           text PRIMARY KEY,
  token_hash   bytea NOT NULL UNIQUE,
  owner        text NOT NULL,
  description  text NOT NULL,
  role         text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text,
  expires_at   timestamptz,
  revoked_at   timestamptz,
  revoked_by   text,
  last_used_at timestamptz
);
CREATE OR REPLACE FUNCTION access.verify_token(digest bytea)
RETURNS TABLE (id text, owner text, description text, role text, created_at timestamptz,
  created_by text, expires_at timestamptz, revoked_at timestamptz, revoked_by text,
  last_used_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  UPDATE access.token t
  SET last_used_at = CASE
    WHEN t.last_used_at IS NULL OR t.last_used_at < now() - interval '5 minutes' THEN now()
    ELSE t.last_used_at END
  WHERE t.token_hash = digest AND t.revoked_at IS NULL
    AND (t.expires_at IS NULL OR t.expires_at > now())
  RETURNING t.id, t.owner, t.description, t.role, t.created_at, t.created_by,
    t.expires_at, t.revoked_at, t.revoked_by, t.last_used_at
$f$;
REVOKE ALL ON FUNCTION access.verify_token(bytea) FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA access TO %I, %I', :'usr', :'adm') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION access.verify_token(bytea) TO %I', :'usr') \gexec
SELECT format('GRANT SELECT, INSERT ON access.token TO %I', :'adm') \gexec
SELECT format('GRANT UPDATE (revoked_at, revoked_by) ON access.token TO %I', :'adm') \gexec

-- Analysis settings changed in the admin GUI (ADR 060): an append-only history, the
-- latest row of a key is its value (NULL = back to the environment or the default). The
-- admin GUI reads and appends; the ingest (ops role) may only call
-- analysis.current_settings().
CREATE SCHEMA IF NOT EXISTS analysis AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS analysis.setting_change (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key        text NOT NULL,
  value      jsonb,
  changed_at timestamptz NOT NULL DEFAULT now(),
  changed_by text,
  reason     text
);
CREATE INDEX IF NOT EXISTS setting_change_key ON analysis.setting_change (key, id);
CREATE OR REPLACE FUNCTION analysis.current_settings()
RETURNS TABLE (key text, value jsonb, changed_at timestamptz, changed_by text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  SELECT c.key, c.value, c.changed_at, c.changed_by FROM (
    SELECT DISTINCT ON (s.key) s.key, s.value, s.changed_at, s.changed_by
    FROM analysis.setting_change s ORDER BY s.key, s.id DESC) c
  WHERE c.value IS NOT NULL
$f$;
REVOKE ALL ON FUNCTION analysis.current_settings() FROM PUBLIC;
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'ops') \gexec
SELECT format('GRANT USAGE ON SCHEMA analysis TO %I, %I', :'adm', :'ops') \gexec
SELECT format('GRANT SELECT, INSERT ON analysis.setting_change TO %I', :'adm') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION analysis.current_settings() TO %I, %I', :'adm', :'ops') \gexec

-- Dismissed findings (ADR 060 point 6): a pair of entities (correlation) or an entity
-- (anomaly, change point) the user does not want to see again, optionally until a date.
-- The admin GUI dismisses and undoes (no DELETE: history stays); the ingest flags the
-- matching findings `dismissed` after every run through analysis.active_dismissals().
CREATE TABLE IF NOT EXISTS analysis.finding_dismissal (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  finding_type      text NOT NULL,
  entity_id         text NOT NULL,
  related_entity_id text,
  reason            text,
  until             timestamptz,
  dismissed_at      timestamptz NOT NULL DEFAULT now(),
  dismissed_by      text,
  revoked_at        timestamptz,
  revoked_by        text
);
CREATE OR REPLACE FUNCTION analysis.active_dismissals()
RETURNS TABLE (finding_type text, entity_id text, related_entity_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  SELECT DISTINCT d.finding_type, d.entity_id, d.related_entity_id
  FROM analysis.finding_dismissal d
  WHERE d.revoked_at IS NULL AND (d.until IS NULL OR d.until > now())
$f$;
REVOKE ALL ON FUNCTION analysis.active_dismissals() FROM PUBLIC;
SELECT format('GRANT SELECT, INSERT ON analysis.finding_dismissal TO %I', :'adm') \gexec
SELECT format('GRANT UPDATE (revoked_at, revoked_by) ON analysis.finding_dismissal TO %I', :'adm') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION analysis.active_dismissals() TO %I, %I', :'adm', :'ops') \gexec
SQL
echo "Postgres roles and databases are in place."
