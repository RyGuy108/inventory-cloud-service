# Operations guide

The repository includes deployment infrastructure and a manual release mechanism. Cloud resources have **not** been provisioned. Cloud rollout, rollback, notifications, and backup restoration still need real environment drills. Terraform apply creates billable AWS resources.

## Local Linux and Docker operation

1. Copy `.env.example` to `.env` and replace all four passwords. These credentials are for the local profile; production requires validated JWTs with the configured issuer, audience, and roles.
2. Run `docker compose up --build -d`, then `./scripts/smoke.sh`.
3. Inspect `docker compose ps` and `docker compose logs --tail=100 app`. Logs should identify requests without exposing credentials or tokens.
4. Stop with `docker compose down`. The database volume survives. `docker compose down --volumes` permanently deletes local data and is only for intentionally resetting a disposable environment.

The API binds to `127.0.0.1:8080`; PostgreSQL binds to `127.0.0.1:5433` for running the application directly on the host. Neither port is exposed to other computers. Compose runs a one-shot migration job before the application; the application uses `inventory_app` and has no schema or deletion privileges. The application container uses a non-root account, read-only filesystem, and temporary writable storage. Liveness is independent of PostgreSQL; readiness includes PostgreSQL with bounded database connection/read timeouts. Changing the `.env` owner password does not change the password inside an existing PostgreSQL volume. See [database permissions and rotation](database.md).

## AWS layout and tradeoffs

`infra/aws` creates an HTTPS Application Load Balancer, ECS Fargate service, encrypted PostgreSQL 17 RDS database on isolated subnets, CloudWatch logs/dashboard/alarms, and database-secret access. HTTP redirects to HTTPS. An existing ACM certificate, DNS hostname, and identity provider are account-specific prerequisites.

Tasks have public IPs for access to ECR, Secrets Manager, CloudWatch, and the identity provider without a NAT gateway. Security groups allow inbound application traffic only from the ALB. RDS has no public address or internet route. Public IPs still incur charges. For private tasks, add NAT or the required VPC endpoints. Defaults use one task and a single-zone database. Set two tasks and `multi_az_database = true` for zone redundancy after reviewing cost.

The image includes the official AWS RDS CA bundle, and cloud JDBC connections use `sslmode=verify-full` to verify both the certificate and database hostname. Rebuild the image when AWS rotates its CA bundle. A separate one-shot migration task receives the current RDS-managed owner secret and runtime password, runs Flyway, and grants the runtime login only SELECT/INSERT/UPDATE on business tables. The web task receives only the runtime password. Its execution role cannot retrieve the owner secret. The runtime secret is an existing same-account/region Secrets Manager JSON secret with a `password` key, using the default Secrets Manager encryption key. Only its ARN enters Terraform.

RDS owner-secret rotation no longer changes the web application's credential: each migration task fetches the current owner secret on launch. Runtime-password rotation still requires a controlled maintenance procedure because ECS-injected environment values do not refresh in running containers. Keep the runtime password stable during ordinary releases. Confirm region availability of the chosen RDS engine and instance class. PostgreSQL tests cover a non-superuser migration owner; an actual RDS run remains unverified.

## Prepare infrastructure and encrypted state

Requirements: an AWS account and AWS CLI session, Terraform 1.10 or later, Docker, an issued ACM certificate in the deployment region, a hostname you control, and an HTTPS identity provider issuing `aud=inventory-api` and the application's roles claim. No credentials belong in this repository.

1. Estimate ALB, Fargate, public IPv4, RDS, log, and storage costs, and configure an account budget alert.
2. In `infra/bootstrap`, run `terraform init`, then `terraform plan -var='state_bucket_name=YOUR_UNIQUE_BUCKET' -out=reviewed.tfplan`. Review and apply that saved plan when authorized. This creates private encrypted/versioned S3 state storage and an immutable ECR repository. Save bootstrap state securely; it initially lives locally. You can migrate it to a separate key in the new bucket by adding an S3 backend block and running `terraform init -migrate-state` with a separate backend configuration. Never commit state or plans.
3. Copy `infra/aws/backend.hcl.example` to `infra/aws/backend.hcl`. Set the bucket and region. Native S3 locking is enabled with `use_lockfile = true`. Operators need bucket listing and state-object read/write plus lock-object read/write/delete, restricted to the relevant keys.
4. Build, test, and scan the first Linux AMD64 image, then push the exact verified artifact with your normal AWS CLI session. The later OIDC publication role lives in the application stack, so this first publication bootstraps that dependency. Use a unique release tag; ECR tags are immutable. Obtain the digest after pushing.

```sh
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
./mvnw -B -ntp verify
docker buildx build --platform linux/amd64 --load --tag inventory-cloud-service:verified .
verified_image=$(docker image inspect inventory-cloud-service:verified --format '{{.Id}}')
python3 scripts/release_contract.py "$verified_image"
./scripts/verify-release.sh inventory-cloud-service:verified
docker tag "$verified_image" "$ECR_REPOSITORY_URI:$RELEASE_TAG"
docker push "$ECR_REPOSITORY_URI:$RELEASE_TAG"
aws ecr describe-images --repository-name inventory-cloud-service --image-ids "imageTag=$RELEASE_TAG" --query 'imageDetails[0].imageDigest' --output text
```

5. Create a dedicated runtime password secret using Secrets Manager in the same account/region. Generate at least 32 random characters, store JSON with a `password` key, and record only its ARN. Use the default Secrets Manager encryption key. Copy `terraform.tfvars.example` to `terraform.tfvars` in `infra/aws`. Set the secret ARN, existing image URL with `@sha256:...`, certificate, hostname, issuer, audience, and optionally existing SNS notification topics and GitHub role inputs. Leave `desired_count = 0` for initial provisioning. Never put password values in Terraform variables or command history.
6. From `infra/aws`, run `terraform init -backend-config=backend.hcl`, `terraform validate`, then `terraform plan -out=reviewed.tfplan`. Review and apply the saved plan when authorized. Commit `.terraform.lock.hcl` provider lock files, but never credentials, state, or plans.
7. Point the hostname's DNS CNAME or alias at the `load_balancer_hostname` output. Configure release environment variables below. Run `scripts/migrate-ecs.sh` and, only if it succeeds, `scripts/deploy-ecs.sh`, or use the combined deployment workflow. The first successful release starts the service from zero tasks. Verify TLS and run `./scripts/smoke.sh https://YOUR_HOSTNAME`.

S3 state and ECR have Terraform destruction protection. RDS has Terraform and AWS deletion protection and requires a final snapshot. Teardown requires a deliberate data-retention decision, removing appropriate protections, and choosing a unique final snapshot name if the default exists. Do not delete state or backups to fix a Terraform error.

## CI and releases

`Verify` runs Java 21 unit and real PostgreSQL Testcontainers tests on Linux, validates Compose, builds a Linux AMD64 container, scans packaged dependencies and the exact image, emits a CycloneDX SBOM, exercises migration/release failure handling with simulated AWS calls, and formats/validates Terraform without cloud credentials. HIGH/CRITICAL findings (including unfixed findings), missing package inventories, and scanner errors block publication. GitHub actions are pinned by verified commit SHA; Dependabot checks Maven, Docker, actions, and Terraform weekly. Run offline script checks with `python3 -m unittest discover -s scripts -p 'test_*.py'`; these do not prove actual AWS recovery. See [release security](release-security.md).

The manual `Publish verified image` workflow reuses CI, transfers a saved image archive with checksum and image-ID verification, and publishes without rebuilding. Its separate OIDC role has repository-scoped ECR push permissions and no ECS access. Configure `AWS_PUBLISH_ROLE_ARN` from `github_publish_role_arn`. The returned digest is the input to deployment. No ECR image publication has yet been performed.

The manual `Deploy existing image` workflow's default `deploy` operation first runs a migration task using the selected digest and requires a confirmed zero exit status, then promotes that same image. Its explicit `rollback` operation preserves the applied schema and grants as described below. Configure a GitHub `production` environment with required reviewers and permitted branch `main`. Restrict repository write access. Provide an existing GitHub Actions OIDC provider ARN to Terraform to create roles restricted to that exact repository and environment. The deployment role has no ECR push, direct secret-reading, database, or infrastructure-apply permissions. It can pass the migration execution role and run the migration family, so release approval also authorizes schema changes.

Both migration and deployment commands first inspect the selected image's embedded release contract. The gate pulls only its immutable ECR digest, verifies Linux AMD64 and the matching repository digest, and reads the contract without running the application. It requires separate migrations, restricted runtime permissions, and reservation idempotency. It also verifies any live rollback baseline, including running/pending tasks during scale-down. Missing contracts, unsupported capabilities, or uncertain service state block changes. The deployment role has narrowly scoped ECR read access for these checks, and temporary registry credentials are removed afterward. This compatibility declaration does not replace build tests, security scans, or release approval.

Use one release operator at a time and the workflow's existing deployment concurrency group. The scripts preserve the checked rollback baseline and stop if its task definition or desired count changes before a mutation. ECS does not offer an atomic compare-and-swap deployment here; avoid concurrent manual service changes. Images built before the embedded-contract baseline cannot be selected or relied on for automatic rollback.

Set GitHub environment variables:

| Variable | Value |
| --- | --- |
| `AWS_REGION` | Deployment region |
| `AWS_DEPLOY_ROLE_ARN` | `github_deploy_role_arn` output |
| `ECS_CLUSTER` | `ecs_cluster` output |
| `ECS_SERVICE` | `ecs_service` output |
| `ECR_REPOSITORY` | Repository name, e.g. `inventory-cloud-service` |
| `PUBLIC_BASE_URL` | `public_base_url` output |
| `MIGRATION_TASK_DEFINITION` | `migration_task_definition` output; refresh after Terraform changes |
| `ECS_DESIRED_COUNT` | One to four running tasks; defaults to one |

Dispatch from `main` with `operation: deploy` and the verified digest. Migration scheduling errors, failed/missing exit codes, and uncertain completion block the application rollout. The deployment script records the previous task definition and desired count, waits for stability, confirms that the intended revision remained active, and checks HTTPS readiness. Failed checks restore both the previous revision and count, including zero on a failed first release. ECS also has a deployment circuit breaker; its rollback needs a previous completed deployment, so first-deployment failures still require investigation. A migration may have changed the schema before a later release failure; image rollback does not undo that change.

Terraform ignores service task-definition revisions and desired counts controlled by release automation. For infrastructure changes that alter a task definition, first update `container_image` to the currently verified digest, apply the reviewed plan, refresh `MIGRATION_TASK_DEFINITION`, then explicitly deploy the new runtime `task_definition` output and verify stability and smoke checks. Otherwise those task configuration changes do not reach running tasks. Runtime scaling uses the release workflow count or an explicit ECS operation.

## Rollback and migrations

For manual rollback, dispatch the same workflow with `operation: rollback` and a previously verified digest compatible with the current schema, runtime permissions, and API guarantees. This skips migrations and role provisioning: older provisioning code could remove grants needed by containers still serving during the rollout. Images from before the separate migration/runtime-permission baseline are not eligible. Once clients rely on `Idempotency-Key`, rollback images must also support keyed retries; older binaries silently ignore that header. The workflow retains its normal digest promotion, readiness, and service-capacity recovery checks. Record the incident time, failed digest, and restored task definition. Verify readiness and an authenticated product/reservation/cancellation workflow; readiness alone cannot prove business correctness.

Use additive migrations compatible with both current and previous application versions. Add structures, deploy compatible code, backfill separately, and remove obsolete structures only after the rollback window. Reverting an image does not revert its database migrations. Never modify applied Flyway migrations or run Flyway clean against persistent environments.

Required drill: in a disposable cloud environment, deploy an unhealthy image and verify the previous healthy revision returns. Then demonstrate rollback of a schema-compatible application change without losing reservations.

The [local rollback drill](rollback-validation.md) performs the corresponding image replacement with an intentionally unhealthy disposable candidate, an additive fixture migration, and restoration of the exact previous image. It verifies records, retry keys, cancellation, and preservation of the applied schema. It does not establish ECS circuit-breaker, load-balancer, or cloud recovery behavior.

## Monitoring and recovery

CloudWatch retains application logs for 30 days and shows request count, server errors, p95 response time, database connections, CPU, and memory. Alarms detect five server errors in five minutes or no healthy targets for two minutes. `alarm_actions` defaults to empty: alarms are visible but send no notifications until an SNS destination and confirmed subscription are configured. The application provides protected Prometheus metrics; this Terraform does not provision a Prometheus scraper.

The [business counters](business-metrics.md) distinguish committed stock movements from successful retries and exclude rolled-back work. The [monitoring drill](monitoring.md) uses real Prometheus, a separate readiness probe, and Alertmanager to verify firing and resolved notifications at a private receiver. It confirms that database readiness can fail while metrics and liveness remain available. Its accelerated alert intervals and local Basic authentication are fixture settings, not production defaults.

For readiness failures, inspect ECS deployment events, application logs, database state, security groups, connection counts, and identity-provider availability. Keep liveness independent of downstream systems to avoid restart loops. The JDBC read timeout bounds blackholed database requests; exhausted pools also return within the configured connection timeout. For a controlled runtime credential rotation, plan a maintenance window, change the runtime secret, run the migration job to update its database login, and immediately replace web tasks. Existing connections may continue during rotation, but new connections using the old password fail; zero-downtime rotation needs a future dual-login design.

RDS retains automated backups for seven days. Restore a point in time into a **new** private database, attach a disposable application instance, and verify the schema version, inventory totals, reservations, and cancellation behavior. Record restore duration and data-loss window. Never run a recovery drill by replacing the only live database.

Still to demonstrate in AWS: authenticated workflow smoke tests, notification delivery, image rollback, backup restore, concurrent load, sustained latency/error targets, credential rotation, and actual cost. Static Terraform validation does not establish these results.

Repeatable **local** backup/restore, outage, and bounded load checks are implemented and recorded separately in [recovery/performance](recovery-performance.md). They use their own temporary resources, preserving the Compose database, and do not establish cloud recovery timing or capacity.

## Deployment preflight and repeatable local drills

Copy `infra/aws/preflight.example.json` to the ignored `infra/aws/preflight.local.json` and replace the identifiers with your selected account's values. The file accepts only documented non-secret settings; provide the runtime secret ARN, never its password. Run offline validation first:

```sh
python3 scripts/cloud-preflight.py --config infra/aws/preflight.local.json
```

The default command makes no AWS requests. With an existing operator AWS CLI session, add `--aws-read-only` to check account identity, ECR image existence, certificate coverage/expiry, runtime-secret metadata, protected state storage, availability zones, and the optional GitHub OIDC provider. It never retrieves secret contents or creates resources. A successful metadata check still does not certify the deployment: Terraform planning, image verification, actual migrations, HTTPS routing, identity-provider configuration, and live operational drills remain required. The current [readiness report](../reports/cloud-readiness.json) records missing configuration without assuming an AWS account.

The manual GitHub `Operational drills` workflow reuses the test/security pipeline, loads its verified image archive with checksum and image-ID validation, and runs seven drills sequentially on a fresh Linux runner: real Keycloak TLS authentication, restricted-database restore/outage, runtime-password rotation, bounded load, two-replica concurrency/restart, local image rollback, and monitoring/notification delivery. It requires no AWS credentials, publishes no image, and uploads sanitized reports even after failed drills. The [corrected hosted run](https://github.com/RyGuy108/inventory-cloud-service/actions/runs/34898295610) passed all seven drills on native Linux AMD64. The first run’s image-selection failure and the corrected results are retained in [GitHub evidence](../reports/github/README.md). See [replica validation](replica-validation.md) for shared-database correctness checks and their limits.

For equivalent local checks, build the image, make the official fixture images available, and follow [identity-provider validation](oidc-validation.md), [recovery/performance](recovery-performance.md), [password rotation](credential-rotation.md), [rollback validation](rollback-validation.md), and [monitoring](monitoring.md). These commands create and clean up isolated fixture resources. They preserve the existing Compose application and its credentials. The [cloud launch input guide](cloud-launch-inputs.md) explains which remaining values are user choices and which are bootstrap outputs.

## References

- [ECS deployment circuit breaker](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html)
- [Terraform S3 backend and locking](https://developer.hashicorp.com/terraform/language/backend/s3)
- [Official Eclipse Temurin image definitions](https://github.com/docker-library/official-images/blob/master/library/eclipse-temurin)
