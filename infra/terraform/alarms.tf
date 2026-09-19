# The same alert policy as monitoring/prometheus/alerts.yml, expressed as
# CloudWatch alarms. Model signals come from the monitor (namespace FraudMLOps),
# service signals from the load balancer. Every alarm notifies one SNS topic.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  model_alarms = {
    # name = [metric, statistic, comparison, threshold, periods to breach, description]
    stuck-feature      = ["StuckFeatures", "Maximum", "GreaterThanOrEqualToThreshold", 1, 1, "A feature holds one repeated value: upstream fault, fix data, do not retrain"]
    feature-drift      = ["FeaturesDrifting", "Maximum", "GreaterThanOrEqualToThreshold", 1, 2, "A feature's PSI is above its calibrated threshold"]
    prediction-drift   = ["ScorePSI", "Minimum", "GreaterThanThreshold", 0.1, 2, "Score distribution shifted (normal PSI 0.01 to 0.02)"]
    flag-rate-collapse = ["FlagRateRatio", "Maximum", "LessThanThreshold", 0.2, 2, "Model flags under 20% of its training rate: it may have gone blind"]
    flag-rate-spike    = ["FlagRateRatio", "Minimum", "GreaterThanThreshold", 5, 2, "Model flags over 5x its training rate: attack or broken feature"]
    recall-degraded    = ["LabelledRecall", "Maximum", "LessThanThreshold", 0.5, 2, "Recall on labelled traffic under 0.5 (release: 0.70)"]
  }
}

resource "aws_cloudwatch_metric_alarm" "model" {
  for_each = local.model_alarms

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value[5]
  namespace           = "FraudMLOps"
  metric_name         = each.value[0]
  statistic           = each.value[1]
  comparison_operator = each.value[2]
  threshold           = each.value[3]
  evaluation_periods  = each.value[4]
  period              = 60
  # No value means the monitor could not judge (too little traffic or labels), not a breach.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "monitor_stale" {
  alarm_name          = "${local.name}-monitor-stale"
  alarm_description   = "The drift monitor stopped reporting (monitoring the monitor)"
  namespace           = "FraudMLOps"
  metric_name         = "MonitorHeartbeat"
  statistic           = "SampleCount"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  period              = 60
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "latency_p99" {
  alarm_name          = "${local.name}-latency-p99"
  alarm_description   = "p99 target response time above 100 ms. Covers single and batch calls together, since the ALB cannot split by path."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p99"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0.1
  evaluation_periods  = 3
  period              = 60
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "errors_5xx" {
  alarm_name          = "${local.name}-5xx"
  alarm_description   = "More than 1% of requests failing with a 5xx from the API"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "rate"
    expression  = "100 * errors / MAX([requests, 1])"
    label       = "5xx percent"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      stat        = "Sum"
      period      = 60
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      stat        = "Sum"
      period      = 60
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  alarm_name          = "${local.name}-unhealthy-targets"
  alarm_description   = "An API task is failing its load balancer health check"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 2
  period              = 60
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
