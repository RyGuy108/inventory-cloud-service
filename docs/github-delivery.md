# GitHub delivery

The selected GitHub owner is **RyGuy108**. The standalone repository is
[RyGuy108/inventory-cloud-service](https://github.com/RyGuy108/inventory-cloud-service),
created with private visibility.
The repository root is this project directory; the surrounding workspace and its Git
history are not part of the project. See [verification](verification.md) for the recorded
publication and workflow status.

## Initial repository contents

Commit the application and tests in `src/`, Maven wrapper scripts and configuration,
`pom.xml`, `Dockerfile`, `compose.yaml`, database provisioning helpers, operational
scripts, monitoring templates, documentation, and sanitized reports. Include the
`.github/` workflows, Dependabot configuration, ignore and line-ending rules,
`release-contract.json`, `.env.example`, Terraform source, example settings, and both
Terraform provider lock files.

The existing ignore rules exclude the real `.env`, `.local/` database backups and
fixtures, `target/` build output, Python caches, editor settings, Terraform provider
downloads, state, plans, real variable files, `backend.hcl`, and
`infra/aws/preflight.local.json`. The Docker build context also excludes local private
files, reports, documentation, infrastructure, and operational scripts. Example
settings contain placeholders and do not grant access to an account.

The publication review found no local password values, credential patterns, private
keys, machine-specific home paths, or symlinks in the reviewed source and reports.
Historical reports retain their actual image identities, timestamps, and limitations.
The relative backup filename in the upgrade report identifies a private local backup;
the backup itself is excluded.

## Verification before AWS setup

The **Verify** workflow runs for source/configuration pushes and all pull requests.
Pushes changing only documentation or recorded reports do not rebuild the application;
this lets a later evidence commit record the exact source commit and its completed run.
It uses Java 21 and Maven
on an Ubuntu runner, starts disposable PostgreSQL through Testcontainers, checks local
configuration and operational tests, builds a Linux AMD64 image, checks its embedded
release contract, and scans the packaged dependencies and exact image. A separate job
validates Terraform and runs tests with mocked AWS resources. These jobs require no
AWS credentials or deployed service.

The first GitHub run establishes Linux AMD64 evidence independently of the existing
local ARM64 reports. Maven, container images, Terraform providers, GitHub actions, and
the scanner's vulnerability database require their normal download services to be
available. Security findings or verification failures stop the workflow; historical
local success does not override a new failure.

The manually started **Operational drills** workflow first calls **Verify**, then
loads its exact verified image archive and performs the isolated runtime drills. It
checks the archive checksum and image identity before running the fixtures. Test and
security evidence is retained for 30 days; the image archive used within a release or
drill run is retained for three days. Inspect a run's artifacts for its own results.

## Later cloud releases

**Publish verified image** and **Deploy existing image** are manual workflows limited
to `main` and the `production` environment. Creating the GitHub repository does not
create AWS resources or configure this environment. Set its branch restrictions and
the environment variables described in [operations](operations.md) after choosing the
AWS destination and completing the bootstrap.

Publication uses a dedicated AWS role to push the exact image already tested and
scanned. Deployment uses a separate role and requires a verified immutable ECR digest;
deployments run migrations before updating the service. Rollback checks image
compatibility and preserves the schema. GitHub uses short-lived AWS credentials
through OIDC; account credentials, application passwords, and Terraform state do not
belong in the repository.

The remaining AWS account, address, identity-provider, budget, and alert choices are
described in [cloud launch inputs](cloud-launch-inputs.md). Keep their status separate
from GitHub verification and from local operational evidence.
