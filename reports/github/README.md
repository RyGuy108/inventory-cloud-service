# GitHub delivery evidence

Repository: [RyGuy108/inventory-cloud-service](https://github.com/RyGuy108/inventory-cloud-service), private.
These reports describe native Linux AMD64 GitHub runners. They are separate from the
local ARM64 Docker evidence elsewhere in `reports/`. No AWS resources were created and
no image was published to ECR by these workflows.

| Checkpoint | Source commit | Outcome |
| --- | --- | --- |
| [First Verify run](initial-verification.json) | `c19ae9d6854f` | 63 Java, 121 operational, and five mocked Terraform tests passed; build, embedded contract, security scan, and SBOM passed |
| [First operational run](first-operations/README.md) | `c19ae9d6854f` | Authentication fixture setup failed because a moved version tag did not provide the required pinned image; the other six drills passed |
| [Corrected Verify run](verification.json) | `0c0ba5d92517` | The same test and image gates passed after setup was changed to pull the exact image reference selected by the authentication drill |
| [Corrected operational run](operations.json) | `0c0ba5d92517` | All seven live drills passed against the exact verified archive; all fixture cleanup succeeded |

Each JSON record includes the full source commit, run URL, and image identity. The
first operational reports were preserved byte for byte, including the failed OIDC
report. Different workflow builds have different image identities; do not substitute
one scan for a different build's results. The complete current operational JSON reports
are in [operations/](operations/README.md), and that run's scan and SBOM are in
[operations-security/](operations-security/summary.json).

Original GitHub test and security artifacts are retained for 30 days. Image archives
used by the release and operational workflows are retained for three days. Selected
sanitized reports are kept here with source control. Raw downloaded logs, test XML,
and working artifacts remain under ignored `target/` locally.

See [the complete verification record](../../docs/verification.md) and
[GitHub delivery instructions](../../docs/github-delivery.md) for the recorded scope
and the AWS work that still requires a selected account.
