output "api_url" {
  value = "http://${aws_lb.main.dns_name}"
}

output "loadtest_url" {
  value = "http://${aws_lb.main.dns_name}:8080"
}

output "loadtest_token" {
  value     = random_password.loadtest_token.result
  sensitive = true
}

output "ecr_api_repo" {
  value = aws_ecr_repository.api.repository_url
}

output "ecr_loadgen_repo" {
  value = aws_ecr_repository.loadgen.repository_url
}

output "db_host" {
  value = aws_db_instance.main.address
}

output "app_db_secret_arn" {
  value = aws_secretsmanager_secret.app_db.arn
}

output "admin_db_secret_arn" {
  value = local.admin_secret_arn
}

output "seal_bucket" {
  value = aws_s3_bucket.seals.bucket
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "subnets" {
  value = aws_subnet.public[*].id
}

output "task_security_group" {
  value = aws_security_group.tasks.id
}

output "loadgen_task_definition" {
  value = aws_ecs_task_definition.loadgen.family
}

output "alarm_topic" {
  value = aws_sns_topic.alerts.arn
}

output "github_ci_role_arn" {
  value = var.github_repo == "" ? null : aws_iam_role.github_ci[0].arn
}
