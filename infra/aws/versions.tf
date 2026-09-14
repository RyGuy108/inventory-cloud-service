terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}
provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "inventory-cloud-service", Environment = var.environment, ManagedBy = "Terraform" }
  }
}
data "aws_availability_zones" "available" { state = "available" }
data "aws_caller_identity" "current" {}
