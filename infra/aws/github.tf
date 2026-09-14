resource "aws_iam_role" "github_deploy" {
  count = var.github_repository == null ? 0 : 1
  name  = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.github_oidc_provider_arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:production"
        }
      }
    }]
  })
  lifecycle {
    precondition {
      condition     = var.github_oidc_provider_arn != null
      error_message = "Set github_oidc_provider_arn to the existing GitHub OIDC provider ARN."
    }
    precondition {
      condition     = var.github_oidc_provider_arn == "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      error_message = "The GitHub OIDC provider must be the expected provider in this AWS account."
    }
  }
}
resource "aws_iam_role_policy" "github_deploy" {
  count = var.github_repository == null ? 0 : 1
  role  = aws_iam_role.github_deploy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = aws_ecs_service.app.id },
      # These ECS API operations do not support resource-level task family restrictions.
      { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"], Resource = "*" },
      {
        Effect    = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.migration_execution.arn, aws_iam_role.task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      {
        Effect    = "Allow", Action = ["ecs:RunTask"]
        Resource  = replace(aws_ecs_task_definition.migration.arn, "/:[0-9]+$/", ":*")
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.app.arn } }
      },
      {
        Effect   = "Allow", Action = ["ecs:DescribeTasks", "ecs:StopTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.app.name}/*"
      },
      {
        Effect   = "Allow", Action = ["ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${split("@", split(".amazonaws.com/", var.container_image)[1])[0]}"
      },
      # Docker pulls the exact candidate and rollback image to inspect their embedded contracts.
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" }
    ]
  })
}
resource "aws_iam_role" "github_publish" {
  count              = var.github_repository == null ? 0 : 1
  name               = "${local.name}-github-publish"
  assume_role_policy = aws_iam_role.github_deploy[count.index].assume_role_policy
}
resource "aws_iam_role_policy" "github_publish" {
  count = var.github_repository == null ? 0 : 1
  role  = aws_iam_role.github_publish[count.index].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect = "Allow"
        Action = ["ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${split("@", split(".amazonaws.com/", var.container_image)[1])[0]}"
      }
    ]
  })
}
