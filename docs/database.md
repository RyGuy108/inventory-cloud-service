# Database migrations and runtime permissions

The application connects as `inventory_app`. It can select, insert, and update `products` and
`reservations`, including the row locks used when reserving stock. It cannot delete records,
create or alter tables, create temporary tables, drop or truncate tables, assume the migration
owner role, or read or modify Flyway's schema history. UUID keys require no sequence grants.

The database owner (`inventory` locally, `inventoryadmin` on AWS) is used only by the one-shot migration job and operational
database administration. A dedicated database is assumed: provisioning revokes public database
privileges, public table privileges, and public schema creation. Other consumers of this
database need their own explicit grants. Runtime-role provisioning also rejects object
ownership by `inventory_app` and removes any role memberships from that account.

## Local development and existing databases

Set four different passwords in the ignored `.env`: `DATABASE_PASSWORD` (existing database
owner), `DATABASE_APP_PASSWORD` (at least 16 characters), `APP_AUTH_ADMIN_PASSWORD`, and
`APP_AUTH_CUSTOMER_PASSWORD`. Keep the existing owner password for an existing volume.

```sh
docker compose up --build -d
docker compose ps -a
```

Compose waits for PostgreSQL, runs the `migrate` job, and starts the app only after successful
migration and runtime-role provisioning. The job exits with code zero on success. It does not
start a web server, JPA, or authentication configuration. A failed migration or provisioning
step exits nonzero and prevents application startup. Check it with `docker compose logs migrate`.

The existing `inventory-db` volume and its business rows are retained. The original versioned
migration is unchanged. Repeating the job validates migration history, applies only new
migrations, and reapplies explicit runtime permissions. Do not remove the volume to perform
an upgrade. To rerun preparation explicitly before replacing the app:

```sh
docker compose run --rm migrate
docker compose up -d app
```

The application image provides the same interface in local development and ECS:

| Variable | Migration job | Application |
| --- | --- | --- |
| `SPRING_PROFILES_ACTIVE` | `migrate` | `local` or `prod` |
| `DB_URL` | PostgreSQL JDBC URL | Same database URL |
| `DB_USERNAME` | `inventory` locally; `inventoryadmin` on AWS | `inventory_app` |
| `DB_PASSWORD` | Owner password | Runtime password |
| `DB_APP_PASSWORD` | Runtime password, required | Not provided |

Keep production credentials in the cloud secret store. Owner credentials must not be injected
into the service's long-running task. Changing a password also requires updating the matching
secret and restarting affected clients; environment variables on existing containers do not change.

## Manual role provisioning

When a PostgreSQL client is available, `db/provision-runtime.sh` reapplies the same SQL used by
the migration job. First apply the schema migrations, then provide `PGHOST`, `PGPORT`,
`PGDATABASE`, `PGUSER`, owner `PGPASSWORD`, and runtime `DATABASE_APP_PASSWORD` through the
environment and run `sh db/provision-runtime.sh`. Passwords are not passed in command arguments
or printed by the script. Avoid shell tracing and SQL parameter logging when handling secrets.

The shared provisioning SQL lives at `src/main/resources/db/provision/runtime-role.sql`,
outside the Flyway migration directory. Runtime grants list the two business tables explicitly;
new tables remain private until their permissions are reviewed. The job must complete both
Flyway and provisioning before a release is promoted. If provisioning fails after a schema
migration succeeds, fix the cause and rerun the job; do not undo or edit applied migrations.

## Verification

`DatabasePermissionsIntegrationTest` starts disposable PostgreSQL 17 with Testcontainers,
executes the real Spring migration mode, and connects using the runtime credentials to verify
allowed business writes, denied destructive actions, private migration history, and preservation
of existing rows when preparation repeats. It also proves that the runtime account cannot run
the migration job. The ordinary API integration suite covers business rules and transactions.
An additional disposable-database test provisions twice as a database owner with `CREATEROLE`
and without PostgreSQL superuser privileges. This checks compatibility with restricted database
administrators; it does not replace a deployment check on actual Amazon RDS.

For rollback, keep schema migrations backward compatible with the previous application image.
Rolling back an image does not roll back its database schema. Follow `docs/operations.md` for
release rollback and database recovery procedures.
