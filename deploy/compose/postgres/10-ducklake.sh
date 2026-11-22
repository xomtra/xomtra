#!/bin/sh
# Creates the DuckLake catalog database and its owner role on first start.
set -eu
: "${DUCKLAKE_DB:?}" "${DUCKLAKE_USER:?}" "${DUCKLAKE_PASSWORD:?}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db="$DUCKLAKE_DB" -v usr="$DUCKLAKE_USER" -v pwd="$DUCKLAKE_PASSWORD" <<'SQL'
CREATE ROLE :"usr" LOGIN PASSWORD :'pwd';
CREATE DATABASE :"db" OWNER :"usr";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
SQL
