# Inventory Cloud Service

A Java backend for managing products, available stock, and customer reservations. PostgreSQL transactions and row locks prevent competing requests from overselling inventory or restoring cancelled stock twice.

Built with Java 21, Spring Boot 4.1.1, Maven, Spring Security, PostgreSQL 17, Flyway, JUnit, Testcontainers, Docker, GitHub Actions, and Terraform.

## Current delivery

The local application includes the complete create → restock → reserve → cancel workflow, safe keyed reservation retries, role and ownership checks, real PostgreSQL integration tests, production JWT authentication, API documentation, structured logs, and health/metrics endpoints. A separate migration job prepares the database; the running service has only business-table read/insert/update permissions. Release workflows test and scan an image before publishing that exact artifact. Repeatable local backup/restore, outage, rollback, monitoring, and bounded load drills record their results. The private source repository is [RyGuy108/inventory-cloud-service](https://github.com/RyGuy108/inventory-cloud-service). AWS resources have not been created.

See [project milestones](docs/project-plan.md), [architecture](docs/architecture.md), and [operations](docs/operations.md) for the remaining deployment work and operational limits.

The [verification record](docs/verification.md) distinguishes passing local checks from cloud work that still needs to be demonstrated.

## Start locally

Install Docker with Compose and start its engine. Docker builds and runs Java inside the container; a host Java installation is only required for running Maven directly.

```sh
# From this project directory; preserve an existing .env.
test -f .env || cp .env.example .env
```

On a fresh setup, replace all four example passwords in `.env` with different random values. Local account passwords must contain at least 12 characters and at most 72 UTF-8 bytes; `DATABASE_APP_PASSWORD` needs at least 16 characters. This workspace already has a private `.env` containing generated local credentials; it is excluded from Git and Docker build context.

```sh
docker compose up --build -d --wait
./scripts/smoke.sh
```

Open [the local API guide](http://localhost:8080), [OpenAPI](http://localhost:8080/openapi.yaml), or [health](http://localhost:8080/actuator/health).

Run `python3 scripts/demo-local.py` to exercise a complete workflow using the private local credentials. It leaves one demo product and a cancelled reservation in the local database and never prints passwords.

The application binds to `127.0.0.1:8080` and PostgreSQL to `127.0.0.1:5433`. Compose starts PostgreSQL, waits for the one-shot migration job to finish, and starts the service as `inventory_app`. Stop with `docker compose down`; the database volume remains. Changing `DATABASE_PASSWORD` does not rotate an existing database owner's password. See [database setup](docs/database.md) before rotating the runtime password.

## Try the workflow

Local accounts are `admin` and `customer`. Use the corresponding password in `.env` when curl prompts; passwords do not need to appear in command history.

Create stock as the administrator:

```sh
curl --user admin -H 'Content-Type: application/json' \
  -d '{"sku":"WIDGET-01","name":"Widget","initialQuantity":10}' \
  http://localhost:8080/api/v1/products
```

Use the returned product ID to reserve two units as the customer:

```sh
curl --user customer -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: REPLACE_WITH_NEW_RANDOM_UUID' \
  -d '{"productId":"REPLACE_WITH_PRODUCT_ID","quantity":2}' \
  http://localhost:8080/api/v1/reservations
```

Keep the same key and request body when retrying after a timeout. The first request returns 201; replays return 200 with the original reservation's current state. A changed product or quantity under the same key returns 409. See [retry semantics](docs/idempotency.md).

Use the returned reservation ID to cancel. Repeating cancellation returns the cancelled reservation without adding stock again:

```sh
curl --user customer -H 'Content-Type: application/json' -d '{}' \
  http://localhost:8080/api/v1/reservations/REPLACE_WITH_RESERVATION_ID/cancel
```

| Method | Endpoint | Access |
| --- | --- | --- |
| GET | `/api/v1/products` | Authenticated |
| POST | `/api/v1/products` | Administrator |
| GET | `/api/v1/products/{id}` | Authenticated |
| POST | `/api/v1/products/{id}/stock` | Administrator; positive quantity |
| GET | `/api/v1/reservations` | Current customer's reservations |
| POST | `/api/v1/reservations` | Authenticated; customer identity comes from authentication |
| GET | `/api/v1/reservations/{id}` | Owner |
| POST | `/api/v1/reservations/{id}/cancel` | Owner; JSON content type required |

Lists return `{items, page, size, totalElements, totalPages}`. Pagination starts at zero; `size` is 1–100. Errors use `application/problem+json`, with validation errors, status, detail, instance, and a request ID. Every response includes `X-Request-ID`.

## Verify the implementation

With Java 21 or a compatible newer JDK and a running Docker engine:

```sh
./mvnw -B -ntp verify
```

For Colima, set its Docker endpoint before invoking Maven:

```sh
export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
./mvnw -B -ntp verify
```

Tests provision disposable PostgreSQL containers and apply the real Flyway migrations. They fail if Docker is unavailable. They do not use the local Compose database or an in-memory database substitute.

The suite covers stock constraints, normalized SKU uniqueness, transactional rejection, customer isolation, concurrent reservations, concurrent cancellation, repeated cancellation, JSON-only writes, pagination, error headers, safe request IDs, and protected metrics. Production tests use real RSA-signed JWTs and a local JWKS server to check issuer, audience, expiry, signatures, roles, and ownership.

Database permission tests additionally verify that the runtime cannot change the schema, delete data, read migration history, or assume the migration role. A restricted PostgreSQL owner is tested separately from the container superuser. The same migration job can run repeatedly on existing data.

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
./scripts/verify-release.sh inventory-cloud-service-app
python3 scripts/recovery-drill.py --help
python3 scripts/load-test.py --help
```

The security gate fails on any HIGH/CRITICAL finding, missing package inventories, or scanner failure and produces a CycloneDX software bill of materials. The drill scripts create their own temporary containers and do not interrupt the running Compose database. See [release security](docs/release-security.md) and [recovery/performance results](docs/recovery-performance.md).

The GitHub `Verify` workflow runs the tests on Java 21/Linux and validates the container and Terraform. These workflows are designed for this directory to be the root of a standalone GitHub repository; they have not run on GitHub yet.

The manual `Operational drills` workflow checks the verified image against real Keycloak over TLS, restores a database, rotates its runtime password, runs bounded load, verifies two application instances sharing one database, rolls back an unhealthy local candidate, and exercises monitoring alerts on a fresh Linux runner. Local equivalents and evidence are documented in [identity-provider validation](docs/oidc-validation.md), [password rotation](docs/credential-rotation.md), [recovery/performance](docs/recovery-performance.md), [replica validation](docs/replica-validation.md), [rollback validation](docs/rollback-validation.md), and [monitoring](docs/monitoring.md). A read-only [deployment preflight](docs/operations.md#deployment-preflight-and-repeatable-local-drills) reports missing cloud prerequisites before any provisioning.

## Authentication and configuration

The default profile is `prod`; no demo passwords are supplied by default. Production requires an HTTPS OIDC issuer and access tokens with the configured audience. Tokens use `sub` as customer identity and a `roles` array, such as `["ADMIN"]`, for administrator access. Identity-provider setup and token issuance are external to this service.

| Variable | Purpose |
| --- | --- |
| `SPRING_PROFILES_ACTIVE` | Explicit `local` for demo accounts; `prod` for deployment |
| `DB_URL` | PostgreSQL JDBC URL |
| `DB_USERNAME`, `DB_PASSWORD` | Runtime credentials, or owner credentials in the one-shot migration job |
| `DB_APP_PASSWORD` | Migration job only: provision the runtime login; never passed to the running service under this name |
| `DATABASE_APP_PASSWORD` | Compose-only local runtime secret, supplied as the app's `DB_PASSWORD` |
| `JWT_ISSUER_URI` | Production token issuer |
| `JWT_AUDIENCE` | Expected audience; defaults to `inventory-api` |
| `APP_AUTH_ADMIN_PASSWORD` | Local administrator password |
| `APP_AUTH_CUSTOMER_PASSWORD` | Local customer password |
| `PORT` | Application port; defaults to 8080 |

Only the `test` profile creates the second customer used for isolation tests. The service does not accept browser session cookies; state-changing API calls require JSON, and no cross-origin access is enabled. Local Basic authentication is intended for loopback development.

The `migrate` profile runs Flyway and runtime-role provisioning, then exits without starting HTTP, JPA, or authentication. Ordinary application startup does not run Flyway. Cloud migration and runtime tasks receive separate database credentials through separate execution roles.

## Observe and operate

- `/actuator/health/liveness` checks the application independently of PostgreSQL.
- `/actuator/health/readiness` also checks database connectivity.
- `/actuator/prometheus` exposes JVM, HTTP, and database-pool metrics to administrators.
- Logs are JSON with request IDs and route templates, excluding request bodies, tokens, and query strings.
- Docker runs as a non-root user with resource limits and graceful shutdown.
- Database connection/read timeouts keep readiness failures bounded when the database becomes unreachable.
- [Business metrics](docs/business-metrics.md) distinguish committed reservations and stock movements from successful retries; rolled-back work is not counted.

The [operations guide](docs/operations.md) covers Terraform state, AWS costs, HTTPS, database encryption, manual deployment, image rollback, backups, secret rotation, and required cloud drills.

The [cloud launch inputs](docs/cloud-launch-inputs.md) separate account/domain choices from values produced during bootstrap. No cloud resources are created by local drills or preflight validation.

The current Tomcat patch is explicitly overridden to 11.0.25 to address three critical findings in the original managed version. Weekly dependency-update configuration and fresh release scans help detect later changes; a clean scan is scoped to its recorded database timestamp.

## Business limits

Available stock and individual quantities are capped at 1,000,000. A cancellation that would exceed the stock cap returns 409 and leaves the reservation active. Reservation creation is retry-safe when the same customer sends the same `Idempotency-Key`, product, and quantity; successful keys do not expire. Requests without a key create a new reservation each time. Reservations do not expire automatically in this version. Products and completed reservations remain as records; the API has no destructive deletion endpoint.

## Project layout

```text
src/main/java/.../api/         REST contract and error handling
src/main/java/.../domain/      Inventory invariants
src/main/java/.../service/     Transaction boundaries
src/main/java/.../repository/  PostgreSQL access and row locks
src/main/java/.../config/      Authentication and request logging
src/main/resources/db/        Versioned Flyway migrations
src/main/resources/static/    API guide and OpenAPI specification
src/test/java/                Domain, API, and production-auth tests
infra/                       AWS infrastructure and state bootstrap
.github/workflows/           CI and manual image deployment
docs/                        Architecture, milestones, and operations
```
