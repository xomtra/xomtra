#!/bin/sh
# Idempotently creates the roles and databases of xomtra (runs on every `up`, so that
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
# DuckLake retries a commit when another writer took its snapshot id (the stream and the
# jobs write at the same time), and Postgres would log every retry as an ERROR
# ("duplicate key ... ducklake_snapshot_pkey"). Errors of the catalog reach the services
# as exceptions and are logged there, so the server log keeps only FATAL for it.
psql -v ON_ERROR_STOP=1 -U postgres -d postgres -v db="$DUCKLAKE_DB" <<'SQL'
SELECT format('ALTER DATABASE %I SET log_min_messages = %L', :'db', 'fatal') \gexec
SQL
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

-- Claude connector with Home Assistant sign-in (ADR 081). ha_user: HA users who tried to
-- sign in and the role an admin gave them (NULL = no access; HA administrators always
-- get the admin role). session: one row per sign-in, keyed by the id of the HA refresh
-- token of that sign-in (revoked in HA right after it); it ends at expires_at or when an
-- admin disconnects it. The MCP server may only call the functions below; the admin GUI
-- lists, sets roles, disconnects.
CREATE TABLE IF NOT EXISTS access.ha_user (
  ha_user_id    text PRIMARY KEY,
  name          text NOT NULL,
  is_admin      boolean NOT NULL DEFAULT false,
  role          text,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  changed_at    timestamptz,
  changed_by    text
);
CREATE TABLE IF NOT EXISTS access.session (
  id           text PRIMARY KEY,
  idp          text NOT NULL,
  subject      text NOT NULL,
  name         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  last_used_at timestamptz,
  revoked_at   timestamptz,
  revoked_by   text
);
CREATE OR REPLACE FUNCTION access.ha_user_seen(user_id text, user_name text, admin boolean)
RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  INSERT INTO access.ha_user AS u (ha_user_id, name, is_admin, last_seen_at)
  VALUES (user_id, left(user_name, 200), admin, now())
  ON CONFLICT (ha_user_id) DO UPDATE SET name = EXCLUDED.name, is_admin = EXCLUDED.is_admin,
    last_seen_at = CASE
      WHEN u.last_seen_at IS NULL OR u.last_seen_at < now() - interval '5 minutes' THEN now()
      ELSE u.last_seen_at END
  RETURNING u.role
$f$;
CREATE OR REPLACE FUNCTION access.session_touch(sid text, provider text, sub text,
  user_name text, days integer)
RETURNS TABLE (id text, idp text, subject text, name text, created_at timestamptz,
  expires_at timestamptz, last_used_at timestamptz, revoked_at timestamptz, revoked_by text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  INSERT INTO access.session AS s (id, idp, subject, name, expires_at, last_used_at)
  VALUES (sid, provider, sub, left(user_name, 200),
    now() + make_interval(days => least(greatest(days, 1), 365)), now())
  ON CONFLICT (id) DO UPDATE SET last_used_at = CASE
      WHEN s.last_used_at IS NULL OR s.last_used_at < now() - interval '5 minutes' THEN now()
      ELSE s.last_used_at END
  RETURNING s.id, s.idp, s.subject, s.name, s.created_at, s.expires_at, s.last_used_at,
    s.revoked_at, s.revoked_by
$f$;
CREATE OR REPLACE FUNCTION access.session_check(sid text)
RETURNS TABLE (id text, idp text, subject text, name text, created_at timestamptz,
  expires_at timestamptz, last_used_at timestamptz, revoked_at timestamptz, revoked_by text,
  is_admin boolean, role text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  WITH touched AS (
    UPDATE access.session s SET last_used_at = CASE
        WHEN s.last_used_at IS NULL OR s.last_used_at < now() - interval '5 minutes' THEN now()
        ELSE s.last_used_at END
    WHERE s.id = sid
    RETURNING s.*
  )
  SELECT t.id, t.idp, t.subject, t.name, t.created_at, t.expires_at, t.last_used_at,
    t.revoked_at, t.revoked_by, coalesce(u.is_admin, false), u.role
  FROM touched t LEFT JOIN access.ha_user u ON u.ha_user_id = t.subject
$f$;
REVOKE ALL ON FUNCTION access.session_check(text) FROM PUBLIC;
SELECT format('GRANT EXECUTE ON FUNCTION access.session_check(text) TO %I', :'usr') \gexec
REVOKE ALL ON FUNCTION access.ha_user_seen(text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION access.session_touch(text, text, text, text, integer) FROM PUBLIC;
SELECT format('GRANT EXECUTE ON FUNCTION access.ha_user_seen(text, text, boolean) TO %I', :'usr') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION access.session_touch(text, text, text, text, integer) TO %I', :'usr') \gexec
SELECT format('GRANT SELECT ON access.ha_user, access.session TO %I', :'adm') \gexec
SELECT format('GRANT UPDATE (role, changed_at, changed_by) ON access.ha_user TO %I', :'adm') \gexec
SELECT format('GRANT UPDATE (revoked_at, revoked_by) ON access.session TO %I', :'adm') \gexec

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

-- Home Assistant connection entered in the admin GUI (compose edition, ADR 077): an
-- append-only history, the latest row is the connection. The token is encrypted with
-- XOMTRA_SETTINGS_KEY of the secrets volume. The admin GUI reads and appends; the MCP
-- server and the ingest may only call setup.ha_connection().
CREATE SCHEMA IF NOT EXISTS setup AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS setup.ha_connection_change (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  url        text NOT NULL,
  token_enc  text NOT NULL,
  ha_version text,
  changed_at timestamptz NOT NULL DEFAULT now(),
  changed_by text
);
CREATE OR REPLACE FUNCTION setup.ha_connection()
RETURNS TABLE (url text, token_enc text, changed_at timestamptz, changed_by text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  SELECT c.url, c.token_enc, c.changed_at, c.changed_by
  FROM setup.ha_connection_change c ORDER BY c.id DESC LIMIT 1
$f$;
REVOKE ALL ON FUNCTION setup.ha_connection() FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA setup TO %I, %I, %I', :'adm', :'usr', :'ops') \gexec
SELECT format('GRANT SELECT, INSERT ON setup.ha_connection_change TO %I', :'adm') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION setup.ha_connection() TO %I, %I, %I', :'adm', :'usr', :'ops') \gexec

-- Answer guidelines sets of the admin page Answers (ADR 090). Factory sets live in the
-- code; custom sets here. answer_set: one row per custom set (no DELETE, deleted_at
-- hides it); answer_set_version: append-only history of name, description and text,
-- the latest row is the set; active_change: append-only, the latest row is the active
-- set (factory or custom id). The admin GUI reads and appends; the MCP server may only
-- call guidance.active_set().
CREATE SCHEMA IF NOT EXISTS guidance AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS guidance.answer_set (
  id               text PRIMARY KEY,
  based_on         text,
  based_on_version integer,
  created_at       timestamptz NOT NULL DEFAULT now(),
  created_by       text,
  deleted_at       timestamptz,
  deleted_by       text
);
CREATE TABLE IF NOT EXISTS guidance.answer_set_version (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  set_id      text NOT NULL REFERENCES guidance.answer_set (id),
  name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
  description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 200),
  body        text NOT NULL CHECK (char_length(body) <= 8000),
  changed_at  timestamptz NOT NULL DEFAULT now(),
  changed_by  text
);
CREATE INDEX IF NOT EXISTS answer_set_version_set ON guidance.answer_set_version (set_id, id);
CREATE TABLE IF NOT EXISTS guidance.active_change (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  set_id     text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now(),
  changed_by text
);
-- The active set: its id, and for a custom set its latest name and text. No row: nothing
-- chosen yet or the custom set was deleted (the MCP server then uses the default set).
CREATE OR REPLACE FUNCTION guidance.active_set()
RETURNS TABLE (set_id text, name text, body text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
  WITH a AS (SELECT c.set_id FROM guidance.active_change c ORDER BY c.id DESC LIMIT 1)
  SELECT a.set_id, NULL::text, NULL::text FROM a WHERE a.set_id LIKE 'factory:%'
  UNION ALL
  SELECT a.set_id, v.name, v.body FROM a
  JOIN guidance.answer_set s ON s.id = a.set_id AND s.deleted_at IS NULL
  JOIN LATERAL (SELECT x.name, x.body FROM guidance.answer_set_version x
                WHERE x.set_id = s.id ORDER BY x.id DESC LIMIT 1) v ON true
$f$;
REVOKE ALL ON FUNCTION guidance.active_set() FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA guidance TO %I, %I', :'adm', :'usr') \gexec
SELECT format('GRANT SELECT, INSERT ON guidance.answer_set, guidance.answer_set_version, guidance.active_change TO %I', :'adm') \gexec
SELECT format('GRANT UPDATE (deleted_at, deleted_by) ON guidance.answer_set TO %I', :'adm') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION guidance.active_set() TO %I, %I', :'adm', :'usr') \gexec
SQL
echo "Postgres roles and databases are in place."
