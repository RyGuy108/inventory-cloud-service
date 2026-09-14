# Two-replica correctness and restart validation

The service coordinates reservations through PostgreSQL, so retries and stock changes must remain correct when requests reach different application processes. `scripts/replica-drill.py` verifies that behavior against two real containers sharing one disposable PostgreSQL database. It also stops and restarts one application while the other continues to handle requests.

The fixture runs the shipped one-shot `migrate` profile with database-owner credentials before starting either runtime. Both runtimes use the restricted `inventory_app` account with Flyway disabled. It confirms that this account can authenticate through SCRAM and has no elevated role, schema creation, migration-history read, or product-delete privilege. Both APIs bind to randomly assigned loopback ports and use generated per-run local Basic credentials.

## Checks

1. Eight simultaneous requests alternate between the two containers using the same customer, body, and retry key. Exactly one returns `201`; seven return `200`; every reservation field agrees. Five units are deducted once.
2. Eight independent keys compete for seven units in two-unit reservations. Exactly three reservations succeed. The other five responses must specifically report insufficient stock; an unrelated `409` or a lock timeout fails the check. Both containers report one remaining unit.
3. Eight concurrent cancellation calls return identical cancelled records and restore the five units once.
4. A new active reservation is created, then the first replica is gracefully stopped. The surviving replica replays both active and cancelled keys and commits another reservation while its peer remains stopped.
5. The original container restarts with the exact same image. Both replicas replay the active key, the previously cancelled key, and the write committed during the outage. Another eight simultaneous cancellation calls restore stock once.
6. Both APIs must return exactly the same six complete reservation records: four active and two cancelled. Final available quantities are 19 and 1. Extra records, changed fields, negative stock, and unexpected responses fail the drill.

Every concurrency batch has eight workers, a ten-second rendezvous limit, and a twelve-second timeout for each HTTP request. Requests are made directly to each container; the client deliberately alternates replicas. This is a correctness and restart exercise, not a capacity benchmark or a load-balancer availability test.

## Run locally

Build or load the verified application image and make the PostgreSQL fixture image available first:

```sh
docker pull postgres:17-bookworm
python3 scripts/replica-drill.py \
  --image inventory-cloud-service-app \
  --report reports/replicas/latest.json
python3 -m unittest discover -s scripts -p test_replica_drill.py -v
```

The application and database tags are resolved to immutable local image IDs before fixture startup. The report records those identities, container IDs, host characteristics, migration outcome, per-replica request counts, response counts, restart duration, final invariants, and cleanup outcome. Credentials are not included.

Stop/start operations use an additional runtime allowlist and verify resource ownership, image identity, runtime profile, restricted database user, disabled runtime migrations, expected database URL, and private network. A database or migration container cannot be a restart target. The restarted API port is discovered again because Docker may allocate a different ephemeral port. Cleanup uses the existing fixture's exact tracked names and ownership labels; no Compose resource or shared image is removed.

## Evidence and limits

The latest local result is retained in [the replica report](../reports/replicas/latest.json). Its accompanying [evidence notes](../reports/replicas/README.md) identify the image and observations.

This check proves shared-database behavior between two local application processes using the existing Java implementation. It does not demonstrate AWS availability zones, ALB traffic draining, autoscaling, production JWT operation, a database failover, or continued connectivity through a stable public address. The database remains running during the application restart. Production deployment and infrastructure recovery still require the target account and their own live exercises. See [the cloud launch guide](cloud-launch-inputs.md), [the separate image rollback drill](rollback-validation.md), and [the recovery and performance checks](recovery-performance.md).
