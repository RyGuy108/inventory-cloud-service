# Release verification and publication

The repository has three workflows: **Verify**, **Publish verified image**, and **Deploy existing image**. Cloud publication and deployment are manual, main-branch operations. The workflows are prepared locally; no image has been pushed and no cloud workflow has been run as part of this work.

## Verification gate

Every push and pull request runs Java unit/integration tests, local configuration checks, offline operational-script tests, a Linux amd64 container build, and Terraform validation. `scripts/verify-release.sh` then scans two actual artifacts:

- The executable application JAR, including its packaged runtime dependencies.
- The exact local container image ID, including operating-system packages and embedded Java libraries.

The scanner is Trivy **0.74.0**, downloaded from the official release and verified against a SHA256 value pinned in the script. This avoids downloading an unversioned installer or running an unpinned scanner action. Linux x86_64 and macOS ARM64 scanner hosts are supported. Scanner downloads and database caches remain in a temporary directory; project files and container metadata are scanned locally. Telemetry is disabled.

Both dependency and image checks block release on **HIGH or CRITICAL** findings, including vulnerabilities without a published fix. All severities remain in the JSON reports; medium and low findings still require review. No vulnerability exclusions are configured. Missing databases, failed analysis, unreadable reports, or an empty package inventory fail the gate. A scan passing means no blocking findings were reported by that database snapshot; it is not a claim that the application has no vulnerabilities.

The image report is also converted into a CycloneDX software bill of materials (`sbom.cdx.json`). Scanner/database versions, artifact identity, JAR checksum, logs, full findings, and a concise `summary.json` accompany it. GitHub retains test/security evidence for 30 days even when a gate fails. An image archive is retained for three days only when all application gates pass during publication.

Run the same check locally after building the application and image:

```sh
./mvnw -B -ntp verify
docker compose build --pull app
./scripts/verify-release.sh inventory-cloud-service-app:latest
```

Reports default to `reports/security/latest`. Set `SECURITY_REPORT_DIR` to retain another named run. CI writes reports under `target/security`. The archive analyzer uses Trivy's `rootfs` mode because ordinary source-filesystem analysis can omit JAR archives; the inventory check prevents an empty scan being reported as clean.

The scan covers shipped dependencies, not Maven plugins or test-only dependencies. Dependabot checks Maven dependency declarations, Docker base images, GitHub Actions, and Terraform providers weekly. Changes still require review and passing verification. Updating the pinned Trivy version/checksums is a reviewed maintenance task.

## Publish the artifact that passed verification

The first deployment needs an initial image before the application Terraform stack can create its GitHub roles. Follow the operations guide to create the bootstrap ECR repository, then use an authorized operator's normal AWS CLI session to publish the initial Linux amd64 image. Run the local verification gate before pushing and tag/push that exact image without rebuilding it. After the application stack exists, use the workflow below for subsequent publications. An absent publish role is not a reason to put long-lived AWS credentials into GitHub.

1. Configure the protected GitHub **production** environment to allow `main`, with the repository's intended release reviewers. Configure branch protection for the verification jobs.
2. Set environment variables `AWS_REGION`, `ECR_REPOSITORY`, and `AWS_PUBLISH_ROLE_ARN` from the provisioned infrastructure. The publish role only needs the scoped ECR publication policy; it must not have deployment or database permissions.
3. Run **Publish verified image** on `main`. Its reusable verification workflow runs without cloud credentials or OIDC permissions. No pull-request event can invoke the credential-bearing publication job.
4. After successful application and Terraform verification, a separate job downloads the image archive from the same workflow run. It checks the archive's SHA256 against the verification job output, loads the image, and compares its Docker image ID. It does not check out the repository or run application scripts.
5. Only then does the job obtain temporary AWS credentials through GitHub OIDC, tag the loaded image with the source commit/run/attempt, and push it to ECR. The image is not rebuilt. A rerun gets a distinct immutable tag.
6. The job reads the published manifest digest from ECR and retains `release-manifest.json` for 90 days. The run summary contains the digest accepted by **Deploy existing image**.

The published registry digest and local Docker image ID identify different objects; they need not be equal. The archive checksum and image ID prove which local image was transferred, and the ECR digest identifies its published manifest for deployment. The manifest records both identities and the source commit. This is an artifact integrity trail, not a cryptographic provenance attestation.

OIDC trust must match the exact repository and production environment subject (`repo:OWNER/REPOSITORY:environment:production`) with audience `sts.amazonaws.com`. Enforce allowed branches on the environment because the environment subject replaces the branch in the token. Publication uses its own ECR-only role, separate from the ECS deployment role. No long-lived AWS keys are needed in GitHub.

## Deploy and roll back

Set `AWS_DEPLOY_ROLE_ARN`, `ECS_CLUSTER`, `ECS_SERVICE`, `PUBLIC_BASE_URL`, and `MIGRATION_TASK_DEFINITION` in the production environment, together with the shared AWS/ECR variables. `ECS_DESIRED_COUNT` is optional and defaults to `1`.

Run **Deploy existing image** on `main`, supplying the `sha256:...` digest from the successful publication manifest. Leave `operation` at its default, **deploy**, for a new release. The workflow runs database migrations using that image before updating the service. A migration failure stops deployment. The service deployment then checks the intended task revision and public readiness endpoint; failure restores the previous revision and service capacity.

For an intentional rollback, select `operation: rollback` and supply a previously verified image whose application, API behavior, and runtime database permissions are compatible with the currently applied schema. Images from before the separate migration/runtime-permission baseline are not eligible. Once clients rely on reservation `Idempotency-Key` behavior, images from before idempotency support are also ineligible: they tolerate the additive column but ignore the key. The retry guarantee is available only after every serving task supports it.

Rollback skips migrations and role provisioning: running an older image's provisioning logic could revoke permissions still needed by newer containers during the rollout. The applied schema and grants remain in place while the same digest promotion, readiness verification, and service-capacity recovery checks run. The readiness check requires HTTP 200 and a JSON `status` of `UP`; redirects, an HTML page, and a `DOWN` payload fail verification. The deployment also rejects task definitions without exactly one `inventory` container before registering or updating the service. Database migrations must remain compatible with the preceding application version because application rollback does not reverse schema changes.

### Compatibility enforced from image contents

The image contains `/app/release-contract.json`, copied from the reviewed repository file during its build. The current policy requires exactly schema version `1`, service `inventory-cloud-service`, and these capability identifiers: `separate-migrations/v1`, `restricted-runtime/v1`, and `reservation-idempotency/v1`. CI extracts and checks this contract from the immutable built image before security scanning and publication.

Both `scripts/migrate-ecs.sh` and `scripts/deploy-ecs.sh` invoke `verify-deployment-image.py` before any ECS mutation. The verifier resolves the application ECR repository, pulls the exact requested `repository@sha256:...`, checks that Docker reports that same repository digest and Linux amd64, then extracts the embedded contract without running the application's process. Caller-supplied capability metadata, mutable image tags, and platform or policy override flags are not accepted. Missing, malformed, duplicated-key, oversized, symlinked, or incompatible contracts fail the operation. Each image pull has a two-minute limit so a registry failure stops the gate promptly.

Automatic rollback eligibility is checked before migration or rollout too. The verifier requires one confirmed active service with no AWS failure entries and exactly one `PRIMARY` deployment whose task definition matches the service and whose rollout state is `COMPLETED`. Service and deployment counts must agree, running count must equal desired count, and pending count must be zero. This prevents an in-progress rollout from presenting its new task definition as a safe baseline while an older incompatible image still serves traffic or remains the circuit breaker's rollback target. Mixed deployments, failed/in-progress rollouts, unknown deployment state, and incomplete scaling block the operation before image verification or any ECS mutation.

When that settled baseline has running tasks, its image must pass the same contract gate as the candidate. First deployment remains supported when both the service and its single completed deployment have desired/running/pending counts of zero; no old image contract is required then. Immediately after provisioning, wait for ECS to report that zero-task deployment completed before retrying the first release. Merely setting desired count to zero while tasks or rollout activity remain does not bypass baseline verification.

The deployment consumes the checked task definition, deployment ID, and capacity from the gate's own JSON result as its rollback baseline. Before registering the new revision, it compares a fresh service snapshot with those checked values; migration does the same with the snapshot that supplies its networking before registering or starting its task. A changed task revision, deployment ID, rollout state, deployment inventory, or desired/running/pending count aborts the operation. The scripts never silently substitute a newly observed, unchecked image as the rollback baseline. These read/compare checks are not an atomic AWS compare-and-swap: use one release operator and retain the workflow's deployment concurrency control; do not run competing manual deployment commands.

Extraction uses a stopped container created from the inspected immutable local image ID. A unique operation label, exact name, and image identity must match before that temporary container is removed, including when a create response is uncertain. Registry login data lives only in a private temporary Docker configuration that is removed on success or failure; the operator's saved registry login is unchanged. The deployment IAM role adds only ECR authentication and repository-scoped image/layer reads for this check. It gains no publication permission.

This is a compatibility check of the selected artifact, not a provenance or security attestation. The contract records capabilities asserted by the reviewed release code; the existing behavioral tests, image/dependency scans, immutable publication, and production environment controls remain necessary. Database and API changes still require compatibility review. Existing images without the contract cannot be promoted or used as a live automatic-rollback baseline, even if they happened to implement similar behavior before the metadata was introduced.

## Check deployment prerequisites without making changes

`scripts/cloud-preflight.py` validates a supplied JSON file of non-secret identifiers. It never discovers credentials or configuration automatically in offline mode and never accepts a password, token, or access key as configuration. Copy `infra/aws/preflight.example.json` to a private local file, replace the placeholders, and run:

```sh
python3 scripts/cloud-preflight.py --config /path/to/preflight.local.json
```

With no configuration supplied, the script records the missing prerequisites and exits unsuccessfully. Its report defaults to `reports/cloud-readiness.json`. Offline success means the identifiers are internally consistent, while AWS resources remain unverified. It checks matching account/region across the image, certificate, runtime secret, and optional GitHub provider; Terraform now enforces those same resource boundaries during planning. This prevents cross-region/account inputs from passing validation and later failing because release permissions address a different ECR repository or unsupported secret.

An operator can explicitly request read-only AWS metadata checks using an already authorized AWS CLI session:

```sh
python3 scripts/cloud-preflight.py --config /path/to/preflight.local.json --aws-read-only
```

The allowlist contains only STS caller identity, ECR repository/image descriptions, ACM certificate description, Secrets Manager **DescribeSecret**, IAM OIDC-provider description, S3 versioning/encryption/public-access settings, and available-zone descriptions. An unexpected active account stops further checks. No secret values, Terraform plans/applies, deployments, publications, or resource changes are requested. Raw AWS errors and configuration values are omitted from the report. The default Secrets Manager key is required because the task execution policy does not grant access to a customer-managed KMS key.

Metadata verification checks image existence, immutable tags, an issued/unexpired matching certificate, a current undeleted runtime-secret version, the supported encryption key, private/versioned/encrypted state storage, two available zones, and optional OIDC URL/audience. It cannot prove the secret contains the right password, the image supports amd64 or passes security gates, DNS/identity-provider behavior, database grants, account quotas/capacity, or GitHub environment controls. `deployment_ready` therefore remains false until the deployment and operational checks are independently completed. The report labels successful results as `configuration_valid_unverified` or `metadata_verified`, not as a completed deployment.

The actual report produced in this workspace is **blocked** because the nine required deployment settings have not been supplied. No AWS metadata request was made. Offline tests cover rejected account/region mismatches, secret-field redaction, no implicit AWS calls, exact command allowlisting, account isolation, certificate wildcard boundaries, and unsupported/deleted/unversioned secrets.

## Local evidence

`reports/security/baseline` records the original image and JAR. The scanner found three critical Tomcat vulnerabilities in `tomcat-embed-core` 11.0.24: CVE-2026-65182, CVE-2026-65905, and CVE-2026-68525. Its reported fixed version is 11.0.25. The baseline correctly failed release verification. The full original image report also retains 52 medium and 16 low package findings.

The image with application metrics and the compatibility contract, `sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d`, passed the security gate at `2026-09-14T16:16:19Z`: 102 packaged Java dependencies had no reported vulnerabilities; 245 image packages had zero high/critical findings and 40 medium plus 16 low findings. None of the remaining findings had a fixed version listed in that database snapshot. They remain visible in `reports/security/observability/image.json`; no exclusions were added. The image SBOM contains 246 components, including the operating-system component. Nine offline security-policy tests cover positive verification and fail-closed error/vulnerability cases.

Actual local extraction accepted that new image's contract and rejected the preceding `03e51ec41894...` image, which lacked the embedded contract. No application process was started and no extraction containers remained afterward. `reports/release-compatibility.json` records this evidence. Fifteen focused compatibility tests and the expanded deployment/migration tests cover immutable identity/platform matching, exact capabilities, temporary credential cleanup, ownership-checked container cleanup, incompatible candidates/baselines, completed versus mixed deployments, rollout changes, and settled first-deployment behavior. AWS registry verification has been tested using local fixtures; no AWS request or deployment was performed here.

Read `summary.json` and `scanner.json` for each run's UTC timestamp, database snapshot, artifact identities, inventory counts, and actual gate result. Local scans on this development machine cover Linux arm64 images. The GitHub verification/publication workflow builds and independently scans the Linux amd64 image required by the ECS configuration. The named `baseline`, `latest`, `idempotency`, and `observability` directories retain successive checkpoints; `observability` is the newest application scan. Do not treat an older checkpoint as a report of a newer image.

## Primary references

- [Trivy 0.74.0 release](https://github.com/aquasecurity/trivy/releases/tag/v0.74.0)
- [Trivy Java archive analysis](https://trivy.dev/docs/latest/coverage/language/java/)
- [Trivy SBOM formats](https://trivy.dev/docs/latest/supply-chain/sbom/)
- [GitHub OIDC with AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [GitHub deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
- [AWS Secrets Manager metadata API and default-key behavior](https://docs.aws.amazon.com/cli/latest/reference/secretsmanager/describe-secret.html)
- [ECS service stability waiter](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/ecs/wait/services-stable.html)
- [Terraform tests using mocked providers](https://developer.hashicorp.com/terraform/language/tests/mocking)
- [Docker pulls by immutable digest and platform](https://docs.docker.com/reference/cli/docker/image/pull/)
- [ECR authentication and image-read permissions](https://docs.aws.amazon.com/AmazonECR/latest/userguide/repository-policy-examples.html)
- [ECS deployment status, rollout state, and task counts](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_Deployment.html)

GitHub Actions commit pins were resolved from the official upstream repositories with `git ls-remote` on 2026-09-13. The adjacent version comments describe the corresponding upstream major release tags. Dependabot proposes updates to those pins; review upstream changes before merging them.
