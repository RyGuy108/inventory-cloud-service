# Verification record

Verified September 14, 2026, America/Chicago. Reports record UTC timestamps and immutable image identities so earlier evidence is distinguishable from the current build.

The earlier [observability verification report](../reports/verification-observability.json) ties that checkpoint to its exact image. The current operational suite also includes two-replica concurrency and restart checks.

## Automated checks

| Check | Result |
| --- | --- |
| Maven verification | **63 tests passed**, 0 failed, 0 errors, 0 skipped |
| Domain tests | 19 stock/invariant cases |
| HTTP/PostgreSQL integration tests | 30 cases, including competing reservations/cancellations, keyed retries, key conflicts/isolation, bounded lock contention, committed business counters, rollback suppression, and concurrent replay accounting |
| Production authentication tests | 5 cases using real signed JWTs and a temporary JWKS server |
| Database permission integration tests | 9 cases: allowed writes, denied destructive/administrative access, repeated provisioning, restricted migration owner, and V1→V2 data preservation/unique keys |
| Operational/security script tests | **121 passed**: 21 deployment/rollback, 14 migration, 15 image-contract, 9 security-gate, 14 preflight, 10 drill-fixture, 17 monitoring, 8 local-rollback, and 13 replica cases; AWS/HTTPS interactions simulated |
| Terraform | Both modules validated; formatting and 5 mocked AWS account/region-boundary tests passed |
| Compose, shell configuration, GitHub Actions | Configuration checks and actionlint for all four workflows passed |
| Linux container build | Passed; Temurin Java 21.0.12, Linux ARM64 |
| Local upgrade | Existing V2 schema retained; app and PostgreSQL healthy; all three existing products/reservations retained with matching business-data fingerprints |
| Live local workflow | Create, reserve, keyed replay, changed-payload conflict, cancel, repeated cancellation and replay after cancellation passed using restricted runtime credentials |
| Health and metrics | Liveness/readiness returned UP; all eight protected business counters matched the completed live workflow |

Maven ran on the installed host JDK 26 with Java 21 compilation target. The Docker image independently compiled and ran the application with Java 21. Integration tests used actual PostgreSQL 17 containers. The upgraded Compose application receives only the `inventory_app` credential; the migration job receives the owner and runtime credentials. This upgrade changed no migration versions. A subsequent live demo added a fourth product and cancelled reservation after the upgrade preserved all three earlier products/reservations. See [upgrade evidence](../reports/local-upgrade-observability.json) and [live workflow/metrics evidence](../reports/local-live-observability.json). The private backup from the earlier V1→V2 upgrade remains excluded from Git and Docker build context.

## Security scan

Verified local image: `sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d`.

The initial scan found three critical Tomcat vulnerabilities. Upgrading Tomcat to 11.0.25 removed those findings; installing available base-image updates removed twelve fixed medium findings. The current Trivy scan at `2026-09-14T16:16:19Z` passed with:

- 102 packaged Java dependencies: no reported vulnerabilities.
- 245 image packages: **zero HIGH/CRITICAL**, 40 MEDIUM and 16 LOW findings. None of the remaining findings had a fixed version in that database snapshot; no exclusions were added.
- A CycloneDX SBOM with 246 components, including the operating-system component.

This is a dated scan of Linux ARM64, not a claim that the application has no vulnerabilities. The release workflow independently builds and scans Linux AMD64 and publishes the exact verified image archive without rebuilding. Findings, scanner metadata, image identity, and SBOM are in [security evidence](../reports/security/observability/summary.json); see [release security](release-security.md) for the gate and publication design.

## Current-image monitoring and rollback

These real Docker drills used the current verified image, disposable databases, the one-shot owner migration job, and the restricted runtime login. Their resources were removed successfully; the existing Compose database was untouched by the drills.

| Drill | Observed result |
| --- | --- |
| Protected monitoring | Real Prometheus scraped JVM and business metrics; anonymous access returned 401 and customer access 403 |
| Business counters | Reservation creation, keyed replay, and first cancellation each measured once; the live Compose demo separately checked all eight counters |
| Database outage alert | Readiness probe returned 503, fresh metrics scraping remained successful, liveness stayed 200, and Prometheus fired the readiness alert |
| Notification delivery | Private Alertmanager webhook received the firing notification 17.016 seconds after pausing the database |
| Recovery notification | Matching resolved notification arrived 2.060 seconds after unpause; readiness and a business read recovered |
| Candidate schema | A disposable candidate added one nullable column with a real V3 migration; all existing Java resources and previous migration checksums remained unchanged |
| Image rollback | Candidate business API returned 200 but readiness returned 404 and Docker marked it unhealthy; restoring the exact previous image recovered readiness in 12.781 seconds after failure detection |
| Full replacement interval | 25.600 seconds from stopping the original application through candidate failure and restored readiness, on the same loopback API address |
| Compatibility and data | Six runtime rollback checks passed; records, retry keys, cancellation behavior, and the candidate's additive schema/history survived rollback |

The candidate-only V3 migration was never added to the repository or existing Compose database. These measurements demonstrate one compatible nullable-column change and local Docker replacement, not arbitrary migration safety or ECS recovery timing. Notifications went to a private fixture receiver, not an external contact or production SNS destination. See [monitoring evidence](../reports/monitoring/final.json), [rollback evidence](../reports/rollback/latest.json), [monitoring instructions](monitoring.md), and [rollback instructions](rollback-validation.md).

## Two-instance correctness and restart

The current ARM64 image also passed six real checks with two separate application containers sharing one disposable database. Eight simultaneous same-key requests produced one new reservation and seven identical replays. Competing reservations could not oversell stock, and concurrent cancellations restored units once. While one replica was stopped, the other replayed existing keys and committed a new reservation. The original replica restarted in 13.897 seconds; both then returned the exact six expected reservation records and stock quantities. All disposable resources were removed successfully.

These requests reached the containers directly on loopback ports using generated local Basic credentials. This proves consistency across application processes and one graceful restart; it does not demonstrate ALB routing, production JWT operation, autoscaling, or database failover. See [replica evidence](../reports/replicas/latest.json) and [repeatable instructions](replica-validation.md).

## Image compatibility enforcement

Images now embed a strict versioned release contract declaring separate migrations, restricted runtime permissions, and reservation idempotency. CI checks the embedded contract; migration and deployment scripts require compatible immutable Linux AMD64 ECR images and inspect the live rollback baseline before mutation. The service must have exactly one completed primary deployment, with matching desired/running counts and zero pending tasks. The scripts preserve the checked revision, deployment identity, and counts, then reject drift before mutation. Settled zero-task bootstrap skips inspecting the unused previous image. Contract extraction does not start application code, and private registry credentials and extraction containers receive bounded cleanup. Deployment still requires a single operator or the workflow's concurrency control; AWS offers no atomic compare-and-update here.

Actual local inspection accepted the current Linux ARM64 image and rejected the previous image, which lacks the contract. The native local inspection is distinct from the enforced Linux AMD64 cloud gate. The contract declares supported behavior; it is not a signature, vulnerability scan, or provenance attestation. See [compatibility evidence](../reports/release-compatibility.json) and [release controls](release-security.md).

## Earlier-image identity-provider validation

The following identity-provider, backup/restore, password-rotation, and load reports were recorded against the preceding image `sha256:03e51ec41894cd7838d7cf4f8f7887d8e775adbf105274bcd7d6faea263ba30c`. They remain evidence for that checkpoint and were not rerun against the current image. Current Java verification includes five production JWT tests and the current monitoring/rollback drills exercise the new image with PostgreSQL.

All **10 real Keycloak checks passed** using the production application profile, HTTPS discovery/token endpoints, certificate and hostname validation, and the restricted runtime database account. The drill verified signed tokens, administrator/customer roles, reservation ownership/isolation, keyed replay, rejection of Basic/anonymous requests, genuine wrong-audience tokens, modified signatures, and genuine short-lived token expiry after the accepted clock skew. The token was accepted before expiry; a fresh token succeeded after the expired one was rejected. The issuer certificate was rejected without the fixture CA trust.

Keycloak 26.7.3, PostgreSQL, the generated private CA/credentials, and the production-profile app were disposable local fixtures. All owned resources were cleaned up. The existing Compose service continues to use local development authentication. See [OIDC evidence](../reports/oidc/latest.json) and [identity-provider guide](oidc-validation.md); this verifies a real local provider, not the future cloud account's provider configuration.

## Earlier-image recovery and load drills

These drills used the preceding verified image in separate disposable containers, leaving the Compose database intact. They ran the actual one-shot migration job with owner credentials, then used the restricted `inventory_app` login with runtime Flyway disabled. Fresh SCRAM TCP connections verified runtime permissions. Earlier single-owner fixture reports remain historical evidence in the results index.

| Drill | Observed result |
| --- | --- |
| Backup/restore | Restored an 8,290-byte synthetic backup into a new database in 0.112 seconds; restored application ready in 9.514 seconds |
| Restored business data | Products, reservations, and idempotency keys preserved; cancellation restored stock exactly once; source unchanged |
| Database outage | Readiness returned 503 in 5.069 seconds while liveness remained 200; readiness recovered after unpause and a business read passed |
| Runtime-password rotation | Old runtime stopped; migration job rotated the login; fresh old-password connection rejected and new-password connection accepted with restricted privileges |
| Rotation continuity | Replacement ready after a 12.729-second maintenance interval; business data and idempotency keys preserved; cancellation/replays correct |
| Concurrent load | 288 requests, 8 workers, 39.960 seconds: 64 reservations, 96 confirmed insufficient-stock conflicts, 128 reads |
| Load correctness and timing | Zero unexpected errors/overselling; stock 0 and 64 reservations; 7.21 requests/sec; p50 1,087 ms, p95 1,616 ms, p99 1,997 ms |
| Cleanup | All four drills removed their disposable resources without errors |

Measurements use local Basic authentication with one application CPU and 768 MiB, so they establish local correctness and an observed baseline, not sustained production capacity or cloud recovery targets. Rotation deliberately uses a maintenance interval and does not claim zero downtime or AWS secret delivery.

The load run used a 50-second request-issue window and ran after the other Docker work finished. It remains a small local measurement rather than a controlled cross-version benchmark. See [recovery evidence for that checkpoint](../reports/recovery-restricted-runtime.json), [rotation evidence](../reports/credential-rotation-final.json), [load evidence for that checkpoint](../reports/load-restricted-runtime.json), [all run results including historical failures](../reports/README.md), and [repeatable drill instructions](recovery-performance.md).

## Remaining environment verification

GitHub-hosted CI/publication and the manual seven-drill operational workflow, AWS provisioning, actual RDS migration permissions, the selected cloud identity provider, cloud alert delivery, cloud image rollback, RDS point-in-time restore, sustained cloud load, and AWS secret rotation have **not** been executed. No image was published remotely and no cloud resources were provisioned. The [cloud readiness report](../reports/cloud-readiness.json) records nine missing settings and made no AWS requests. Some settings are setup outputs; the [launch-input guide](cloud-launch-inputs.md) separates those from the destination, domain, identity provider, budget, alert route, and repository choices. The [cloud review](../reports/cloud-review.json) records earlier health, task-definition, and account/region checks. Review [operations](operations.md) and [remaining milestones](project-plan.md) before deployment.
