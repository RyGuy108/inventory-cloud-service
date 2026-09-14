variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "environment" {
  type    = string
  default = "demo"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,12}$", var.environment))
    error_message = "Use a short lowercase environment name."
  }
}
variable "container_image" {
  description = "Existing linux/amd64 ECR image URL pinned by sha256 digest."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/.+@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "Supply an existing private ECR image with an immutable sha256 digest."
  }
}
variable "certificate_arn" {
  description = "Issued ACM certificate ARN in the same region, matching public_hostname."
  type        = string
}
variable "public_hostname" {
  description = "DNS name covered by the ACM certificate; point it to the ALB after provisioning."
  type        = string
}
variable "jwt_issuer_uri" {
  description = "HTTPS OIDC issuer, reachable by the application."
  type        = string
  validation {
    condition     = startswith(var.jwt_issuer_uri, "https://")
    error_message = "The production issuer must use HTTPS."
  }
}
variable "jwt_audience" {
  type    = string
  default = "inventory-api"
}
variable "desired_count" {
  description = "Bootstrap count. Leave at zero until the migration task succeeds; release automation sets the live count."
  type        = number
  default     = 0
  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 4 && floor(var.desired_count) == var.desired_count
    error_message = "Choose zero to four tasks; use zero for first provisioning."
  }
}
variable "runtime_database_secret_arn" {
  description = "Existing same-account, same-region Secrets Manager secret containing JSON password for inventory_app. No secret value enters Terraform. Use the default Secrets Manager encryption key."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.runtime_database_secret_arn))
    error_message = "Provide the ARN of the pre-created runtime password secret."
  }
}
variable "multi_az_database" {
  description = "Enable before calling this a highly available production deployment."
  type        = bool
  default     = false
}
variable "alarm_actions" {
  description = "Existing SNS topic ARNs for alarm delivery. Empty means dashboard alarms only."
  type        = list(string)
  default     = []
}
variable "github_repository" {
  description = "Optional owner/repository for a deployment role restricted to the production GitHub environment."
  type        = string
  default     = null
  validation {
    condition     = var.github_repository == null ? true : can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use owner/repository."
  }
}
variable "github_oidc_provider_arn" {
  description = "Existing GitHub Actions OIDC provider ARN, required when github_repository is set."
  type        = string
  default     = null
}
