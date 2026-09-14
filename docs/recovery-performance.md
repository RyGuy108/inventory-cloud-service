# Local recovery and performance drills

These drills exercise the real containerized Java application and PostgreSQL through authenticated HTTP APIs. They use disposable databases and do not stop, modify, back up, or load-test the existing Compose application. They do not create cloud resources.

## Run the drills

Requirements: Python 3, a running Docker engine, and locally available application and database images. Build the application image from the repository when needed:

```sh
docker build --tag inventory-cloud-service-app .
python3 scripts/recovery-drill.py
python3 scripts/load-test.py
```

Both commands resolve the selected application and PostgreSQL image tags to immutable IDs at startup and record those IDs in their JSON reports. Set `--image sha256:...` to select a specific compatible image. The image must provide the one-shot `migrate` profile, runtime-role provisioning, and reservation idempotency support. The default database image is `postgres:17-bookworm`; `--database-image` can select a locally available immutable ID. Images are not pulled automatically by these scripts.

Run the commands sequentially to reduce contention between the drills. Load-test defaults are 288 requests with eight concurrent workers, a 45-second deadline for issuing requests, and a three-second timeout per request. Startup, warm-up, validation, and cleanup are separate from the measured workload and may take longer than a minute. No new measured request starts after the deadline. In-flight requests finish or time out; a workload lasting more than 60 seconds fails.

```sh
python3 scripts/load-test.py --requests 288 --concurrency 8 --duration 45 \
  --report reports/load-latest.json
```

Reports are written even when a drill fails. A failed assertion, unexpected HTTP/transport error, unfinished workload, or cleanup error produces a nonzero exit status. The load report includes per-request timings, HTTP status, operation, and aggregate p50/p95/p99 latencies using the nearest-rank method. Throughput includes both successful requests and expected stock conflicts. Generated credentials are never included in reports.

A deadline failure means the requested sample count was not completed within the chosen request-issue window; it does not by itself imply a business correctness failure. Preserve that report before rerunning. On this shared development host, the final-image 45-second run completed 284 of 288 requests without unexpected errors. A separate run used `--duration 50`; both reports are retained in the results summary.

## Restore verification

The recovery drill:

1. Creates a private, uniquely named Docker network and PostgreSQL container. It runs the image's real one-shot migration job as the owner, requires a confirmed successful exit, checks the restricted `inventory_app` login, and starts the application with that runtime credential and Flyway disabled. Both images are pinned to their inspected IDs. Only the API gets a random loopback port; database ports are not published.
2. Creates 37 units of inventory and reserves seven units using the public API with an idempotency key.
3. Runs `pg_dump --format custom` against this disposable database. The backup remains in process memory, and the report records its size and SHA-256 hash.
4. Starts a separate, fresh PostgreSQL container and restores the backup through standard input using `pg_restore --exit-on-error`.
5. Runs the migration job against the restored database to validate history and provision its runtime login and explicit grants. PostgreSQL roles are not carried in this per-database backup, and the backup intentionally omits grants. It then starts another restricted application instance, compares every returned product and reservation field with the source, cancels the reservation twice, and verifies stock returns to 37 exactly once. Replaying the same creation key before and after cancellation must return the original reservation with HTTP 200 and never reserve new stock. It also verifies the source database still has the original 30 available units.
6. Pauses only the restored database, verifies readiness returns HTTP 503 while liveness returns HTTP 200, then unpauses it and verifies readiness and a business read recover.

The databases use tmpfs storage. Pausing preserves those contents; stopping a tmpfs-backed database would discard the data. This simulates a temporarily unresponsive database, not a database restart or storage failure.

The fixture explicitly configures PostgreSQL JDBC `socketTimeout=5` and `connectTimeout=3` so an unresponsive database produces a bounded failure. The report records this override alongside the runtime user, disabled runtime Flyway setting, and migration-job exit codes.

The fixture generates separate owner and runtime database passwords plus local administrator/customer passwords. PostgreSQL host connections require SCRAM authentication. Runtime credentials are tested through a fresh TCP connection, with checks that the login has no elevated PostgreSQL attributes, schema creation, product deletion, or Flyway-history access. The long-running application receives only the runtime database password; the owner password is used only for preparation and operational backup/restore commands. A failed or timed-out migration blocks runtime startup.

The [credential rotation drill](credential-rotation.md) uses the same fixture and migration job to change the runtime password and replace the application during a deliberate maintenance interval. These local drills do not verify production JWT authentication, cloud TLS, AWS secret delivery, or managed database rotation.

## Workload and accounting checks

The default workload first makes 160 concurrent one-unit reservation attempts against a product with 64 units, then makes 128 authenticated product reads. Eight reads warm up the application first and are excluded from the measurement. Exactly 64 reservations should succeed; the other 96 should return HTTP 409. Every product read must return HTTP 200. The final product quantity must be zero, and the customer must have exactly 64 reservations.

The report distinguishes successful 2xx responses, expected 409 stock conflicts, and unexpected errors. Client timings use a monotonic clock and include connection setup, local HTTP Basic authentication, application processing, and response transfer. Every request opens a new HTTP connection. This workload is deliberately small and includes the cost of local password verification; it is not a prediction of production JWT throughput.

## Resource ownership and cleanup

Every resource name contains a fresh random run identifier. Containers and the network carry both `inventory.drill.run` and `inventory.drill.purpose` labels. Before any destructive operation, the script checks its own tracked resource list, the exact name, and both ownership labels. A mismatch fails closed and is recorded as a cleanup error. There are no wildcard deletions, volume deletions, Compose operations, or database ports exposed to the host.

Normal completion, exceptions, and keyboard interruption execute cleanup. A process forcibly killed by the operating system can leave resources behind. If that happens, inspect the report's run identifier and Docker labels before manually removing any specifically identified disposable containers or network. Never remove the existing Compose database or its persistent volume as part of a drill.

## Evidence and limits

The [restricted-runtime recovery report](../reports/recovery-restricted-runtime.json) records a passing run against image `sha256:03e51ec41894cd7838d7cf4f8f7887d8e775adbf105274bcd7d6faea263ba30c` on September 14, 2026 UTC. Both migration jobs exited zero, runtime permission checks passed, and reservation keys survived restore and cancellation. The 8,290-byte backup restored in 0.112 seconds; the replacement application was ready after 9.514 seconds. The paused database produced readiness 503 while liveness stayed 200. Cleanup completed without errors.

The [restricted-runtime load report](../reports/load-restricted-runtime.json) passed against that same image after other Docker verification and upgrade work completed. It issued 288 requests with eight workers in 39.960 measured seconds: 64 reservations, 96 verified insufficient-stock conflicts, and 128 successful reads. There were zero unexpected errors, zero available units, and exactly 64 reservations at completion. Throughput was 7.207 requests/second; overall client latency was p50 1,086.6 ms, p95 1,615.6 ms, and p99 1,996.8 ms. This single run used a 50-second request-issue window and removed its resources without cleanup errors.

Measured runs and exact image IDs are recorded in [the results summary](../reports/README.md). The application has one CPU and 768 MiB of memory per instance; each database has one CPU, 384 MiB of memory, and a 256 MiB tmpfs filesystem. Host and Docker details are included in each report. Other local workloads can affect the measurements.

Earlier baseline and final-image reports in that summary used the previous single-owner fixture with startup Flyway enabled. They remain historical evidence of that configuration. Current reports explicitly identify the separate migration/runtime model and must not be treated as reproductions of the older fixture.

The restore timing covers this tiny synthetic dataset. It does not establish production recovery time or recovery point objectives, sustained capacity, cloud network behavior, or an availability commitment. Separate [local rollback](rollback-validation.md) and [monitoring](monitoring.md) drills now demonstrate compatible image replacement and private firing/recovery notifications. AWS backup/point-in-time restore, ECS image rollback, production alert routing, credential rotation, and sustained load still need verification in the selected cloud environment.
