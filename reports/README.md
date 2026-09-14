# Local drill results

These are observed local results recorded on September 14, 2026 UTC. Each checkpoint identifies its exact application image. Local image rollback has been demonstrated; no cloud deployment or cloud recovery has been executed.

## Current image: business metrics, monitoring, and release compatibility

Application: `sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d`.

The later [two-replica drill](replicas/latest.json) passed six checks against this same image: concurrent retries created one record, competing instances preserved stock, cancellations restored once, and one instance continued accepting writes while the other restarted. Restart-to-ready took 13.897 seconds. Both instances returned the exact six final records and stock quantities; cleanup succeeded. The expanded offline suite passed all 121 tests, including 13 replica checks.

| Check | Observed result |
| --- | --- |
| [Protected metrics and notifications](monitoring/final.json) | Real Prometheus scrape passed; anonymous/customer access rejected; reservation, replay, and cancellation counters measured |
| Database outage and alert | Readiness 503, fresh metrics available, liveness 200; private firing notification received 17.016 seconds after pause |
| Recovery notification | Matching resolved webhook received 2.060 seconds after unpause; readiness and business read recovered |
| [Local image rollback](rollback/latest.json) | Six checks passed: unhealthy candidate replaced with the exact prior image on the same loopback address |
| Schema and data compatibility | Candidate-only additive V3 migration remained; previous checksums, records, retry keys, cancellation and replay behavior preserved |
| Rollback timing | 12.781 seconds from candidate failure detection to restored readiness; 25.600 seconds for the entire stop/replacement/recovery interval |
| [Embedded release contract](release-compatibility.json) | Current image accepted; previous image without the contract rejected; extraction containers cleaned |
| [Existing service upgrade](local-upgrade-observability.json) | All three existing products/reservations preserved with matching fingerprints; schema remains V2 |
| [Live local workflow](local-live-observability.json) | Healthy current image; create/retry/conflict/cancel behavior passed and all eight business counters matched |
| [Security scan](security/observability/summary.json) | 102 packaged Java dependencies with no findings; image has 0 HIGH/CRITICAL, 40 MEDIUM and 16 LOW findings |
| Cleanup | Monitoring and rollback removed all owned disposable resources without errors; source image and Compose database preserved |

The rollback candidate's V3 migration existed only in a temporary fixture archive. It never entered repository migrations or the existing Compose database. The final live demo added a fourth product and cancelled reservation after the upgrade had preserved all three earlier records. Notifications were delivered only to a private disposable receiver. These results do not establish ECS rollback behavior or production notification routing.

The [combined verification report](verification-observability.json) records 63 Java tests, 108 offline operational/security tests, and 5 mocked Terraform plan tests, with matching image identities across the current evidence. See [the verification record](../docs/verification.md) for interpretation. The manual GitHub workflow now repeats seven drills sequentially against the verified image archive; it has not run remotely. The following older reports remain valid evidence for their recorded images and were not rerun against this checkpoint.

## Previous image: retry support and restricted runtime

Application: `sha256:03e51ec41894cd7838d7cf4f8f7887d8e775adbf105274bcd7d6faea263ba30c`.

These runs use the real one-shot migration profile with owner credentials, then start the application with restricted `inventory_app` credentials and Flyway disabled. Fresh SCRAM TCP connections verify login and privileges. The OIDC drill uses the production application profile and real Keycloak; the recovery, rotation, and load drills use local Basic authentication. All use disposable resources and preserve the existing Compose database.

| Check | Observed result |
| --- | --- |
| [Real identity provider](oidc/latest.json) | 10 checks passed: verified TLS, signed tokens, roles, ownership, keyed replay, wrong-audience/signature rejection, genuine token expiry |
| [Backup/restore](recovery-restricted-runtime.json) | Passed; 8,290-byte backup restored into a fresh database in 0.112 seconds; app ready in 9.514 seconds |
| Restored data | Products, reservations, and retry keys survived; cancellation/replays restored stock exactly once; source unchanged |
| Database outage/recovery | Readiness 503 after 5.069 seconds; liveness stayed 200; readiness and business reads recovered after unpause |
| [Runtime-password rotation](credential-rotation-final.json) | Passed; old password specifically rejected on a new connection; new password authenticated with restricted privileges |
| Rotation maintenance | 12.729 seconds from stopping old runtime through replacement readiness; records and retry keys preserved |
| [Concurrent workload](load-restricted-runtime.json) | Passed; 288 requests, eight workers, 39.960 measured seconds; 50-second request-issue window |
| HTTP outcomes | 64 reservations, 96 exact insufficient-stock conflicts, 128 reads, zero unexpected errors |
| Final inventory | Stock 0, reservations 64; no overselling |
| Load timing | 7.207 requests/second; p50 1,086.6 ms, p95 1,615.6 ms, p99 1,996.8 ms |
| Cleanup | All four drills removed their owned resources without errors |
| [Existing local upgrade](local-upgrade-idempotency.json) | Private backup saved; V1→V2 preserved both earlier products and reservations with matching fingerprints |
| [Security scan](security/idempotency/summary.json) | 0 HIGH/CRITICAL; 40 MEDIUM and 16 LOW findings remain visible |
| [Cloud readiness](cloud-readiness.json) | Blocked by nine missing deployment settings; no AWS requests made |

The private backup of the existing Compose database is stored under ignored `.local/backups/` with restricted permissions; it is not included in these public report artifacts. The separate fixture backup remains in process memory. The final live demo added one product and cancelled reservation after the upgrade verification.

At that checkpoint, verification comprised 59 Java tests, 57 offline operational/security tests, 5 mocked Terraform plan tests, and the real drills above. The manual GitHub workflow then contained four drills and had not run remotely.

## Previous hardened image

Application: `sha256:5618c6248a1c1de311fe52169426653beb0f89ed386497c4d1cb38d1c4f694f6`.

PostgreSQL and resource limits are the same as the initial-image runs below. The fixture explicitly enables Flyway for its own single-owner databases; the final application's normal runtime disables startup migrations. These are measurements on a shared development host with other work taking place, not a controlled benchmark comparison.

| Check | Observed result |
| --- | --- |
| [Backup and restore](recovery-final.json) | Passed; 7,573-byte custom-format backup restored into a separate fresh PostgreSQL database in 0.103 seconds |
| Restored application startup | Ready after 10.493 seconds |
| Restored business data | Product/reservation fields matched, cancellation restored stock exactly once, repeated cancellation was idempotent, and source remained unchanged |
| Database outage | Readiness HTTP 503 after 5.079 seconds; liveness HTTP 200 |
| Database recovery | Readiness recovered 0.006 seconds after unpause; authenticated business read passed |
| [Completed concurrent workload](load-final.json) | Passed; 288 requests, eight workers, 45.804 measured seconds; 50-second request-issue deadline |
| HTTP outcomes | 64 reservations created, 96 conflicts with the exact insufficient-stock response, 128 successful reads, zero unexpected errors |
| Final inventory | Zero available units and exactly 64 reservations; no overselling |
| Throughput | 6.288 requests/second including expected conflicts |
| Overall client latency | p50 1,289.6 ms; p95 1,816.3 ms; p99 2,022.7 ms |
| Cleanup | Both final drills removed their created containers and networks without errors |

The [first final-image load attempt](load-final-deadline.json) failed its 45-second request-issue deadline: 284 of 288 requests completed in 45.548 measured seconds, with zero unexpected errors and correct inventory accounting. That report is retained. The completed run changed only the issue deadline to 50 seconds; the hard measured-workload limit stayed 60 seconds. Neither result establishes a production latency target.

[Three offline ownership checks](drill-safety-checks.json) also passed: a foreign container name is rejected before any Docker call, mismatched ownership labels are rejected, and cleanup reports a label mismatch without issuing a deletion. These are mocked guard checks, separate from the real database and HTTP drills.

## Initial image (historical)

Application: `sha256:a15a6e1cec21fbb87def55c645a92bff59f38e671ec0130711b4c213064c60d0`.

PostgreSQL: `sha256:051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0`.

The Docker host reported Ubuntu 24.04.4 LTS on aarch64, four CPUs, and 6,197,448,704 bytes of memory. Each app instance was limited to one CPU and 768 MiB; each database to one CPU and 384 MiB, using temporary memory-backed storage. JDBC socket/connect timeouts were explicitly set to 5/3 seconds in the fixture. Local Basic authentication was used with newly generated credentials.

| Check | Observed result |
| --- | --- |
| [Backup and restore](recovery-baseline.json) | Passed; 7,574-byte custom-format backup restored into a separate fresh PostgreSQL database in 0.080 seconds |
| Restored application startup | Ready after 10.085 seconds |
| Restored business data | Product and reservation fields matched the source; cancellation returned stock exactly once; source data stayed unchanged |
| Database outage | Readiness became HTTP 503 after 5.078 seconds; liveness stayed HTTP 200 |
| Database recovery | Readiness recovered 0.006 seconds after unpause; authenticated business read passed |
| [Concurrent workload](load-baseline.json) | 288 requests, eight workers, 38.794 measured seconds |
| HTTP outcomes | 64 reservations created, 96 stock conflicts, 128 successful reads, zero unexpected errors |
| Final inventory | Zero available units and exactly 64 reservations; no overselling |
| Throughput | 7.424 requests/second including expected conflicts |
| Overall client latency | p50 1,050.6 ms; p95 1,710.3 ms; p99 1,988.0 ms |
| Cleanup | Both drills removed their created containers and networks without errors |

The initial load run classified expected conflicts by HTTP status and checked the final stock/reservation invariants. The current script additionally verifies each expected 409 has the `Insufficient available stock` response detail, so other conflict causes fail the workload.

These short runs measure a tiny dataset on a shared local Docker host. Password hashing and one new connection per request are included in latency; setup and validation are excluded. The restore time does not establish a production recovery objective. The measurements do not predict JWT-authenticated cloud throughput, sustained capacity, alert delivery, RDS point-in-time recovery, or rollback behavior. See [the drill guide](../docs/recovery-performance.md) for reproduction and scope.
