#!/bin/sh
# Loads the ledger schema (mounted from ledger/schema.sql) after the app role exists.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /schema/ledger.sql
