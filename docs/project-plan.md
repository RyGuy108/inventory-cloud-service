# Project milestones

## Product scope

An inventory reservation API for administrators who add products/stock and customers who reserve and cancel inventory. The key acceptance criteria are preventing overselling, never restoring cancellation stock twice, and keeping each customer's reservations private.

## Phase tracking

| Phase | Current implementation | Remaining evidence or work |
| --- | --- | --- |
| 1. Requirements | Core workflow, permissions, stock bounds, API contract | Choose real deployment account, region, domain, budget, and identity provider |
| 2. Java foundation | Spring Boot 4.1.1, Java 21 target, Maven wrapper; standalone private GitHub repository under RyGuy108; first hosted verification passed | Maintain the source repository and its checks |
| 3. PostgreSQL | Flyway migrations, constraints/indexes, one-shot migration job, restricted runtime role, verified V1→V2 upgrade preserving records | Verify role provisioning on the selected RDS instance |
| 4. REST workflows | Products, restocking, reservations, cancellation, errors, pagination, safe keyed retries across concurrent requests/restores/runtime replacement | Additional domain requirements; enable client reliance on retry keys after all serving tasks support them |
| 5. Tests and security | Hosted Java 21 verification: 63 Java tests, 121 operational tests, AMD64 image/contract/security gates and SBOM; local real-provider and recovery evidence; weekly updates | Configure selected cloud identity provider; review later dependency alerts |
| 6. Docker/Linux | Multi-stage image, local Compose, non-root runtime, health checks | Record an ECS/RDS sustained-load baseline |
| 7. Terraform | AWS environment, migration task, separated roles, encrypted state, enforced account/region boundaries, 5 mocked plan tests, read-only preflight | Choose the launch destination; generate setup outputs, verify actual account metadata, review plan and provision |
| 8. Observability | Request logs, commit-aware business counters, liveness/readiness, real protected Prometheus scraping, local and hosted private firing/recovery notifications, cloud dashboard/alarm definitions | Configure the production alert destination and verify cloud notification delivery |
| 9. Delivery | Verified artifact publication, mandatory candidate/live-baseline compatibility checks, migration-first promotion, strict readiness validation, checked revision/count rollback; all seven operational drills passed on native GitHub Linux | Configure production OIDC/environment controls and demonstrate an AWS image publication/release |
| 10. Recovery/performance | Real local image rollback after an additive migration; restricted-runtime restore/outage/load; maintenance password rotation rejecting old login; two-replica retries, stock contention, cancellation, and restart validation; exact ownership cleanup | Demonstrate ECS image rollback, load-balancer routing, RDS restore, cloud notifications, AWS secret rotation, and sustained-load targets |
| 11. Documentation | README, OpenAPI, architecture, operations, metrics, monitoring, rollback, and launch-input guides; dated evidence tied to exact images | Add final cloud URL and measured operational results after deployment |

## Next deployment milestone

The current [verification record](verification.md) and [cloud readiness report](../reports/cloud-readiness.json) distinguish completed local phases from account-dependent work. The private GitHub repository has been created under **RyGuy108**; no AWS resources have been created.

Use [cloud launch inputs](cloud-launch-inputs.md) to choose the AWS account/region, controlled hostname, identity provider, spending limit, and notification destination. Follow the bootstrap order in [operations](operations.md): review the bootstrap plan, create the state bucket/ECR repository, publish the first compatible verified Linux AMD64 image, prepare the certificate and runtime secret, and review the application plan before provisioning it with zero tasks. Configure publication/deployment roles, run migrations, and deploy. Record successful API, alert, rollback, restore, and rotation drills in that environment. The local reports establish development evidence; cloud capacity and availability targets need measurements in the selected account.
