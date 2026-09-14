# GitHub operational evidence

All seven drills passed in [Operational drills run 34898295610](https://github.com/RyGuy108/inventory-cloud-service/actions/runs/34898295610)
on 2026-09-14, using source commit `0c0ba5d92517aa88d0a6e476ac919a0ccb792650`.
The workflow's own verification jobs passed 63 Java tests with no failures, errors, or
skips, 121 operational Python tests, and five Terraform tests using mocked AWS resources.

Every drill used the same verified Linux AMD64 application image:

`sha256:01930695af4dc945666e61faba4884f1c66c3d22ef224739f69b600c74d1ea73`

The workflow checked the saved image archive's checksum and loaded image identity before
running the fixtures. This run built its own image; its identity differs from the separate
push-triggered Verify run. The [security summary](../operations-security/summary.json)
and [embedded compatibility contract](../operations-security/release-contract.json) refer
to this exact operationally tested image. The scan found no HIGH or CRITICAL findings:
102 dependency packages had no findings, while 245 image packages had 40 MEDIUM and
16 LOW findings. The [SBOM](../operations-security/sbom.cdx.json) contains 246 components.

| Report | Verified result |
| --- | --- |
| [OIDC](oidc.json) | Ten checks with a real Keycloak issuer, verified TLS, production authentication, ownership, retries, and token rejection |
| [Recovery](recovery.json) | Backup restored into a separate database with matching records and retry behavior; database outage affected readiness while liveness remained available |
| [Password rotation](rotation.json) | Old runtime credentials rejected, new credentials accepted, and business records preserved; maintenance took 27.72 seconds |
| [Bounded load](load.json) | 288 requests, zero unexpected errors, 96 expected stock conflicts, no overselling; p95 latency 995 ms for this fixture |
| [Two replicas](replicas.json) | Shared retry keys and stock remained correct; surviving instance accepted writes during peer restart; six exact final reservation records |
| [Image rollback](rollback.json) | Restored the exact prior image in 20.39 seconds after detecting candidate failure; data, retry behavior, and additive schema migration remained intact |
| [Monitoring](monitoring.json) | Protected metrics remained available during a readiness outage; private alert notification arrived in 17.09 seconds and resolved 2.03 seconds after recovery |

All seven reports have empty `cleanup_errors` arrays. The rollback report also confirms
that its temporary candidate image and temporary base tag were removed. OIDC and rollback
use `passed: true`; the other reports use `status: "passed"`. Their original schemas and
bytes are preserved from the workflow artifacts.

These fixtures ran sequentially on a GitHub-hosted Ubuntu runner with two CPUs and about
8 GB of memory. Each application container was limited to one CPU and 768 MB. Measurements
describe that bounded disposable environment. The two-instance test routes directly to
loopback ports and does not exercise an AWS load balancer. Alert delivery uses a private
temporary receiver, and the rollback candidate adds a fixture-only migration to an image.
The backup stays in process memory and is never uploaded. No AWS deployment, real RDS
restore, external alert destination, cloud secret rotation, or production availability
guarantee is established by these results.

The JSON files here are the sanitized original `operational-drills-34898295610-1`
artifact. The adjacent security files come from `verification-34898295610-1`. Raw run
logs, generated passwords and tokens, database backups, and the image archive are not
published in this evidence directory.
