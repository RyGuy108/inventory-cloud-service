resource "aws_cloudwatch_metric_alarm" "target_errors" {
  alarm_name          = "${local.name}-http-5xx"
  alarm_description   = "Application returned at least five server errors in five minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.app.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "healthy_hosts" {
  alarm_name          = "${local.name}-no-healthy-targets"
  alarm_description   = "No healthy application targets for two minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.app.arn_suffix, TargetGroup = aws_lb_target_group.app.arn_suffix }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}
resource "aws_cloudwatch_dashboard" "app" {
  dashboard_name = local.name
  dashboard_body = jsonencode({ widgets = [
    {
      type = "metric", x = 0, y = 0, width = 12, height = 6
      properties = {
        title = "Traffic and server errors", region = var.aws_region, period = 60, stat = "Sum"
        metrics = [
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.app.arn_suffix],
          ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.app.arn_suffix]
        ]
      }
    },
    {
      type = "metric", x = 12, y = 0, width = 12, height = 6
      properties = {
        title   = "Response time p95 (seconds)", region = var.aws_region, period = 60, stat = "p95"
        metrics = [["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.app.arn_suffix]]
      }
    },
    {
      type = "metric", x = 0, y = 6, width = 12, height = 6
      properties = {
        title   = "Database connections", region = var.aws_region, period = 60, stat = "Average"
        metrics = [["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.app.identifier]]
      }
    },
    {
      type = "metric", x = 12, y = 6, width = 12, height = 6
      properties = {
        title = "Application CPU and memory (%)", region = var.aws_region, period = 60, stat = "Average"
        metrics = [
          ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.app.name, "ServiceName", aws_ecs_service.app.name],
          ["AWS/ECS", "MemoryUtilization", "ClusterName", aws_ecs_cluster.app.name, "ServiceName", aws_ecs_service.app.name]
        ]
      }
    }
  ] })
}
