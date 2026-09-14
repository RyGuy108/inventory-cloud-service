mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:owner-AbC123"
        kms_key_id    = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
        secret_status = "active"
      }]
    }
  }
}

variables {
  container_image             = "123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  certificate_arn             = "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012"
  public_hostname             = "inventory.acme.test"
  jwt_issuer_uri              = "https://identity.acme.test/"
  runtime_database_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbC123"
  github_repository           = "owner/inventory"
  github_oidc_provider_arn    = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
}

run "matching_account_and_region" {
  command = plan
}

run "reject_cross_region_image" {
  command = plan
  variables {
    container_image = "123456789012.dkr.ecr.us-west-2.amazonaws.com/inventory@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [aws_ecs_task_definition.app]
}

run "reject_cross_account_runtime_secret" {
  command = plan
  variables {
    runtime_database_secret_arn = "arn:aws:secretsmanager:us-east-1:999999999999:secret:runtime-AbC123"
  }
  expect_failures = [aws_ecs_task_definition.app]
}

run "reject_cross_region_certificate" {
  command = plan
  variables {
    certificate_arn = "arn:aws:acm:us-west-2:123456789012:certificate/12345678-1234-1234-1234-123456789012"
  }
  expect_failures = [aws_lb_listener.https]
}

run "reject_wrong_oidc_account" {
  command = plan
  variables {
    github_oidc_provider_arn = "arn:aws:iam::999999999999:oidc-provider/token.actions.githubusercontent.com"
  }
  expect_failures = [aws_iam_role.github_deploy]
}
