# Local runtime password rotation drill

`scripts/credential-rotation-drill.py` verifies the maintenance procedure for changing the application's PostgreSQL runtime password. It uses the real migration entrypoint, restricted runtime login, and containerized API in an isolated disposable environment. It does not change the existing Compose application, its credentials, or its persistent volume.

## Run

Use Python 3, a running Docker engine, and a locally built application image that provides the `migrate` profile and reservation idempotency support. The PostgreSQL image must also be available locally.

```sh
docker build --tag inventory-cloud-service-app .
python3 scripts/credential-rotation-drill.py \
  --image inventory-cloud-service-app \
  --report reports/credential-rotation-latest.json
```

The script resolves image tags to immutable IDs before starting resources. It records those IDs, Docker host details, migration job results, login checks, business checks, maintenance duration, and cleanup outcomes in the JSON report. Use `--image sha256:...` and optionally `--database-image sha256:...` to select existing immutable images. No images are pulled automatically and no cloud resources are created.

## What it verifies

1. Start a fresh PostgreSQL database with SCRAM host authentication and newly generated credentials. Run the application's one-shot `migrate` profile with owner access to apply the schema and provision `inventory_app`.
2. Probe a fresh TCP runtime connection and verify the account has no elevated PostgreSQL attributes, public-schema creation, product deletion, or Flyway-history access. Start the application as `inventory_app` with Flyway disabled.
3. Create a product with 23 units and an active reservation for six units using an idempotency key. Record all returned product/reservation fields and verify 17 units remain available.
4. Begin a deliberate maintenance interval by stopping only the old disposable application container. Generate a new runtime password, supply it to a new migration job, and require a confirmed successful exit. This reruns schema validation and role provisioning through the same entrypoint used by normal deployments.
5. Check fresh TCP logins using both credentials. The old password must fail specifically with PostgreSQL password-authentication rejection; a network failure, SQL failure, or trusted login does not count as a pass. The new password must authenticate and retain the restricted permissions.
6. Start a replacement application container using the new runtime credential. Verify the original product and active reservation fields are unchanged. Replay the original reservation key and require HTTP 200 with the same reservation. Cancel the reservation twice, replay the creation key again, and verify stock returns to 23 exactly once without creating a new reservation.
7. Remove only the containers and network created by this run. Cleanup requires matching tracked names and both ownership labels, including on a failed drill.

The reported maintenance duration starts immediately before stopping the old runtime and ends when the replacement reports ready. It includes local container shutdown, the migration job, credential probes, and replacement startup. It is a local observation for this tiny dataset, not a production recovery objective or zero-downtime guarantee.

## Failure behavior and secret handling

A failed, timed-out, or unconfirmed migration blocks runtime startup. A password-authentication check cannot pass merely because PostgreSQL is unavailable. The script exits nonzero on any failed requirement and still records the report and attempts owned-resource cleanup.

Passwords are generated for each run and passed through process/container environment variables. They are not placed in command-line arguments, logs emitted by the drill, or reports. No saved user credentials are read. Access to the Docker engine permits inspection of container environment variables; the fixture containers are removed during cleanup. The backup/restore and ownership safeguards are documented in [recovery and performance](recovery-performance.md).

The fixture's offline failure tests run without Docker:

```sh
python3 -m unittest discover -s scripts -p test_drill_fixture.py -v
```

They cover migration failure/timeout, unconfirmed exit state, runtime-start blocking, misleading password probes, elevated privileges, password handling, and refusal to delete resources outside the fixture's ownership.

## Scope

The [recorded rotation run](../reports/credential-rotation-final.json) passed on September 14, 2026 UTC against image `sha256:03e51ec41894cd7838d7cf4f8f7887d8e775adbf105274bcd7d6faea263ba30c`. Both real migration jobs exited zero. PostgreSQL rejected the old password and accepted the new one with restricted permissions. Product/reservation data and the original idempotency key survived runtime replacement; cancellation restored stock exactly once. The measured maintenance interval was 12.729 seconds, and cleanup had no errors.

This verifies local password replacement through the real application migration job and a new restricted runtime process. It does not verify AWS Secrets Manager updates, ECS secret injection or task replacement, RDS-managed owner-password rotation, production JWT authentication, dual-login rotation, or zero downtime. The owner password stays unchanged throughout this drill. There is no automatic production secret rollback.

Existing pooled connections are deliberately excluded from the old-password test: the old runtime is stopped, and every credential probe establishes a new TCP connection. The fixture initializes explicit host authentication and does not use trusted Unix-domain sockets to prove runtime login behavior.

Use the separate [database guide](database.md) and [operations guide](operations.md) when planning real environment changes. This command is a disposable local drill, not a command for rotating production secrets.
