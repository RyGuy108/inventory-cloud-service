#!/bin/sh
# Reapply runtime permissions after migrations. Passwords are read from the environment.
set -eu
: "${DATABASE_APP_PASSWORD:?Set DATABASE_APP_PASSWORD}"
: "${PGPASSWORD:?Set the migration-owner PGPASSWORD}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sql_file="$script_dir/../src/main/resources/db/provision/runtime-role.sql"
psql --no-psqlrc --set=ON_ERROR_STOP=1 --quiet <<SQL
\\getenv runtime_password DATABASE_APP_PASSWORD
BEGIN;
SELECT set_config('inventory.runtime_password', :'runtime_password', true) AS ignored \\gset
\\unset runtime_password
\\include '$sql_file'
COMMIT;
SQL
