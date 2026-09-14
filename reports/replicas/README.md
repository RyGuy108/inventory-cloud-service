# Local two-replica evidence

[latest.json](latest.json) passed on September 14, 2026, from 21:04:59 to 21:06:01 UTC.

- Application image: `sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d`.
- Host: local ARM64 Docker, four CPUs and approximately 6 GiB of memory. Each application replica was limited to one CPU and 768 MiB; PostgreSQL used a separate 384 MiB limit and disposable tmpfs storage.
- Two independently running application containers shared one database. The real migration job succeeded, and a fresh TCP login confirmed the runtime account's restricted permissions.
- Eight concurrent requests using one key produced one `201` and seven `200` responses with identical reservation fields. Stock decreased once.
- Eight competing two-unit requests against seven units of stock produced three successful reservations and five explicit insufficient-stock responses. One unit remained; no overselling occurred.
- Eight concurrent cancellation requests restored stock once. The same behavior held after a replica restart.
- The first replica stopped in 0.272 seconds and became ready 13.897 seconds after restart began. Its peer replayed active and cancelled keys and accepted a new reservation while the first container was stopped.
- Both replicas returned the exact six final reservation records: four active and two cancelled. Final stock was 19 and 1 across the two products.
- All temporary containers and the fixture network were removed. Cleanup reported no errors. Existing Compose resources and their data were untouched by the drill.

The 13 focused offline tests in `scripts/test_replica_drill.py` also passed. They cover runtime stop/start guards, image and network identity, loopback bindings, port changes after restart, bounded simultaneous dispatch, full-field retry comparison, and rejection of unrelated conflicts as overselling evidence.

The measured restart duration is a local observation. Requests were routed directly to each container, and Docker changed the restarted container's ephemeral host port. This is not evidence of an ALB maintaining a stable address, cloud autoscaling, production identity-provider operation, or multi-zone availability. See [the reproducible drill and its limits](../../docs/replica-validation.md).
