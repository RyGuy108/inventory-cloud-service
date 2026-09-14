# First hosted operational run: six passes, one fixture preparation failure

[Operational drills run 34897369630](https://github.com/RyGuy108/inventory-cloud-service/actions/runs/34897369630) completed with a failure on September 14, 2026. It ran source commit `c19ae9d6854f86f13ba759214422970abf4ee8e3` from `main`, from 21:11:24 to 21:23:04 UTC.

The six executed application drills all identify the same verified AMD64 image: `sha256:b838eac092e9b0fb364eaf1f1c5d1025710245e4c9be8e689a8868867102f192`. The OIDC fixture failed before its application-image metadata was recorded and before any authentication check ran.

| Report | Outcome | Observed result |
| --- | --- | --- |
| [oidc.json](oidc.json) | Failed during preparation | Required pinned Keycloak image was not available locally; zero authentication checks ran. |
| [recovery.json](recovery.json) | Passed | Restored all business fields and retry behavior; database outage failed readiness while liveness remained up. |
| [rotation.json](rotation.json) | Passed | Runtime password rotation preserved records and retry behavior; replacement ready in 26.047 seconds from stopping the old runtime. |
| [load.json](load.json) | Passed | 288 requests, 64 reservations, 96 expected stock conflicts, zero unexpected errors; no overselling. |
| [replicas.json](replicas.json) | Passed | Shared keys and stock remained correct across two replicas; restart ready in 20.125 seconds. |
| [rollback.json](rollback.json) | Passed | Exact prior image restored readiness in 19.915 seconds from candidate-failure detection; data and additive schema remained intact. |
| [monitoring.json](monitoring.json) | Passed | Actual protected metric scraping, outage alert, and matching resolved notification; recovery notification arrived in 2.024 seconds. |

Every report records an empty `cleanup_errors` list. Measurements describe this GitHub runner and are not production service targets.

## Exact preparation failure

The OIDC report and failed-step log agree:

```text
Docker image failed: Error response from daemon: No such image: quay.io/keycloak/keycloak:26.7.3@sha256:ff4257d0d64efbe99ed1ddfaf07765cc3c36dc7518bf8324d41961327f441c54
```

The workflow pulled the mutable `26.7.3` tag while the drill inspected a pinned index. Registry inspection confirmed that the tag had moved to `sha256:29be7252db0a106f1cd2ac17b9a56ff2668073da645638a38b9fc67deeb2d6c4`. The required `ff4257d0…` index remained available and includes both Linux AMD64 and ARM64 manifests. This was a mismatch between preparation and the fixture's required image; it provides no evidence of a TLS or authentication defect.

The corrective run is [34898295610](https://github.com/RyGuy108/inventory-cloud-service/actions/runs/34898295610). Its results are recorded separately; this folder preserves the first attempt unchanged.

These seven JSON files are byte-for-byte copies from artifact `operational-drills-34897369630-1`. They were checked for credential fields, raw JWTs, private-key blocks, and authorization headers before inclusion. Raw failed-step logs remain in the ignored local `target/github-first-operations/` directory and are not published here.
