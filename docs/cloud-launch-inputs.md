# Inputs for the first cloud launch

The local implementation and drills can run without a cloud account. Launching the AWS
environment needs the following choices and access. Do not put access keys, tokens or
passwords in chat or in the repository.

| Decision | What to provide or configure |
| --- | --- |
| AWS destination | Expected account ID, region, and an existing authorized AWS CLI/SSO profile on the deployment machine |
| Public address | A hostname you control and access to manage its DNS |
| Identity provider | An HTTPS issuer configured to issue the `inventory-api` audience, a `sub` claim, and an application `roles` array |
| Spending limit | A monthly budget for the selected AWS configuration; review the actual plan and current regional prices before applying |
| Alert destination | An SNS topic and confirmed subscription, or the intended destination to configure during setup |
| Source repository | Selected: private [RyGuy108/inventory-cloud-service](https://github.com/RyGuy108/inventory-cloud-service); see [GitHub delivery](github-delivery.md) |

The expected account ID is checked against the operator's active AWS session. This project
does not discover credentials or select an account automatically. Use the existing Git
identity when publishing the standalone repository.

## Values produced during setup

Some fields in the preflight file are setup outputs, not choices the user must invent:

| Output | Where it comes from |
| --- | --- |
| State bucket and ECR repository | The reviewed bootstrap Terraform plan |
| First image digest | Build, test, scan, compatibility-check, and publish the Linux AMD64 image to that ECR repository |
| ACM certificate ARN | An issued certificate covering the selected hostname in the deployment region |
| Runtime database secret ARN | A dedicated Secrets Manager secret created for the runtime password; only its ARN enters Terraform |
| GitHub role ARNs and ECS identifiers | The application Terraform stack, initially created with zero running tasks |
| Public load-balancer hostname | The application stack output used for DNS routing |

The [operations guide](operations.md) describes the bootstrap order and manual first-image
publication before the application stack's GitHub publication role exists. The read-only
preflight is useful after these prerequisites exist: it checks supplied identifiers and,
when explicitly requested, their AWS metadata. It does not create missing prerequisites.

## Completion criteria

Review the actual Terraform plan and recurring cost, deploy the compatible verified image,
and record production HTTPS authentication, readiness, alert delivery, image rollback,
RDS restore, and secret-rotation results. Local Prometheus/Keycloak/Docker drills establish
development evidence; the selected AWS account still needs its own end-to-end verification.

The private GitHub repository has been created. The AWS account/profile, region, domain,
identity provider, budget, and alert destination remain undecided; no cloud resources have
been created. The current [preflight report](../reports/cloud-readiness.json) records missing
deployment inputs without making AWS requests.
