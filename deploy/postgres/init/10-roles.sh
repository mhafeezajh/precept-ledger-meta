#!/bin/sh
# Creates the app role. Its table grants (INSERT + SELECT only) are in ledger/schema.sql.
set -eu
psql -v ON_ERROR_STOP=1 -v pw="$PL_APP_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE pl_app LOGIN PASSWORD :'pw';
SQL
