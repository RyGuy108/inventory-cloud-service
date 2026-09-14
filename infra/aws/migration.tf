resource "aws_iam_role" "migration_execution" {
  name               = "${local.name}-migration-execution"
  assume_role_policy = local.task_trust
}
resource "aws_iam_role_policy_attachment" "migration_execution" {
  role       = aws_iam_role.migration_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "migration_secrets" {
  role = aws_iam_role.migration_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["secretsmanager:GetSecretValue"]
      Resource = [aws_db_instance.app.master_user_secret[0].secret_arn, var.runtime_database_secret_arn]
    }]
  })
}
resource "aws_ecs_task_definition" "migration" {
  family                   = "${local.name}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.migration_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name            = "migration", image = var.container_image, essential = true, user = "10001"
    stopTimeout     = 35
    linuxParameters = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    environment = [
      { name = "SPRING_PROFILES_ACTIVE", value = "migrate" },
      { name = "DB_URL", value = "jdbc:postgresql://${aws_db_instance.app.endpoint}/inventory?sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem" },
      { name = "DB_USERNAME", value = aws_db_instance.app.username }
    ]
    secrets = [
      { name = "DB_PASSWORD", valueFrom = "${aws_db_instance.app.master_user_secret[0].secret_arn}:password::" },
      { name = "DB_APP_PASSWORD", valueFrom = "${var.runtime_database_secret_arn}:password::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.app.name, awslogs-region = var.aws_region, awslogs-stream-prefix = "migration" }
    }
  }])
}
