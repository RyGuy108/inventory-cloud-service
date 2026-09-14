locals {
  task_trust = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}
resource "aws_ecs_cluster" "app" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = local.task_trust
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "database_secret" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.runtime_database_secret_arn }]
  })
}
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = local.task_trust
}
resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  lifecycle {
    precondition {
      condition     = startswith(var.container_image, "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/")
      error_message = "The ECR image must be in this AWS account and region; release IAM policies are scoped to that repository."
    }
    precondition {
      condition     = startswith(var.runtime_database_secret_arn, "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:")
      error_message = "The runtime secret must be in this AWS account and region."
    }
  }
  container_definitions = jsonencode([{
    name            = "inventory", image = var.container_image, essential = true, user = "10001"
    portMappings    = [{ containerPort = 8080, protocol = "tcp" }]
    stopTimeout     = 35
    linuxParameters = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    environment = [
      { name = "SPRING_PROFILES_ACTIVE", value = "prod" },
      { name = "SPRING_FLYWAY_ENABLED", value = "false" },
      { name = "DB_URL", value = "jdbc:postgresql://${aws_db_instance.app.endpoint}/inventory?sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem" },
      { name = "DB_USERNAME", value = "inventory_app" },
      { name = "JWT_ISSUER_URI", value = var.jwt_issuer_uri },
      { name = "JWT_AUDIENCE", value = var.jwt_audience }
    ]
    secrets = [{ name = "DB_PASSWORD", valueFrom = "${var.runtime_database_secret_arn}:password::" }]
    healthCheck = {
      command  = ["CMD-SHELL", "curl --fail --silent http://localhost:8080/actuator/health/liveness || exit 1"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 120
    }
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.app.name, awslogs-region = var.aws_region, awslogs-stream-prefix = "app" }
    }
  }])
}
resource "aws_lb" "app" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.load_balancer.id]
  drop_invalid_header_fields = true
}
resource "aws_lb_target_group" "app" {
  name                 = local.name
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.app.id
  deregistration_delay = 30
  health_check {
    path                = "/actuator/health/readiness"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  lifecycle {
    precondition {
      condition     = startswith(var.certificate_arn, "arn:aws:acm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:certificate/")
      error_message = "The ALB certificate must be in this AWS account and region."
    }
  }
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
resource "aws_lb_listener" "redirect" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
resource "aws_ecs_service" "app" {
  name                               = local.name
  cluster                            = aws_ecs_cluster.app.id
  task_definition                    = aws_ecs_task_definition.app.arn
  desired_count                      = var.desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  health_check_grace_period_seconds  = 180
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "inventory"
    container_port   = 8080
  }
  # CI owns application revisions after initial provisioning. Terraform owns infrastructure.
  lifecycle { ignore_changes = [task_definition, desired_count] }
  depends_on = [aws_lb_listener.https, aws_iam_role_policy.database_secret, aws_iam_role_policy_attachment.execution]
}
