resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # saves cost; the service publishes its own metrics
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${local.name}"
  retention_in_days = 3
}

locals {
  db_env = [
    { name = "DB_HOST", value = aws_db_instance.main.address },
    { name = "DB_PORT", value = tostring(aws_db_instance.main.port) },
    { name = "DB_NAME", value = aws_db_instance.main.db_name },
    { name = "DB_SSLMODE", value = "require" },
    { name = "AWS_REGION", value = var.region },
  ]
  app_db_secrets = [
    { name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.app_db.arn },
  ]
  app_db_user = { name = "DB_USER", value = "fraud_app" }

  logs = { for c in ["migrate", "api", "monitor", "auditor", "loadgen"] : c => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.ecs.name
      awslogs-region        = var.region
      awslogs-stream-prefix = c
    }
  } }
}

# ---- API task: migration runs first as an init container, then the service starts.
# Only the migrate container receives the admin credentials.

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name             = "migrate"
      image            = local.api_image
      essential        = false
      command          = ["python", "-m", "fraud_mlops.audit.migrate"]
      environment      = local.db_env
      logConfiguration = local.logs["migrate"]
      secrets = [
        { name = "DB_ADMIN_USER", valueFrom = "${local.admin_secret_arn}:username::" },
        { name = "DB_ADMIN_PASSWORD", valueFrom = "${local.admin_secret_arn}:password::" },
        { name = "APP_DB_PASSWORD", valueFrom = aws_secretsmanager_secret.app_db.arn },
      ]
    },
    {
      name             = "api"
      image            = local.api_image
      essential        = true
      dependsOn        = [{ containerName = "migrate", condition = "SUCCESS" }]
      portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
      environment      = concat(local.db_env, [local.app_db_user, { name = "WORKERS", value = "2" }])
      secrets          = local.app_db_secrets
      logConfiguration = local.logs["api"]
      healthCheck = {
        command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status != 200)"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    },
  ])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Roll back automatically if a new image never becomes healthy.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.operator, aws_lb_listener.loadtest]
}

# ---- drift monitor

resource "aws_ecs_task_definition" "monitor" {
  family                   = "${local.name}-monitor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.monitor.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "monitor"
    image     = local.api_image
    essential = true
    command   = ["sh", "-c", "unset PROMETHEUS_MULTIPROC_DIR && exec python -m fraud_mlops.monitoring.monitor"]
    environment = concat(local.db_env, [
      local.app_db_user,
      { name = "CLOUDWATCH_NAMESPACE", value = "FraudMLOps" },
      { name = "MONITOR_INTERVAL_SECONDS", value = "15" },
    ])
    secrets          = local.app_db_secrets
    logConfiguration = local.logs["monitor"]
  }])
}

resource "aws_ecs_service" "monitor" {
  name            = "monitor"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.monitor.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  depends_on = [aws_ecs_service.api]
}

# ---- auditor: seals the log and anchors seals in S3

resource "aws_ecs_task_definition" "auditor" {
  family                   = "${local.name}-auditor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.auditor.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "auditor"
    image     = local.api_image
    essential = true
    command   = ["sh", "-c", "unset PROMETHEUS_MULTIPROC_DIR && exec python -m fraud_mlops.audit.seal --loop"]
    environment = concat(local.db_env, [
      local.app_db_user,
      { name = "SEAL_ANCHOR_BUCKET", value = aws_s3_bucket.seals.bucket },
      { name = "SEAL_INTERVAL_SECONDS", value = "30" },
    ])
    secrets          = local.app_db_secrets
    logConfiguration = local.logs["auditor"]
  }])
}

resource "aws_ecs_service" "auditor" {
  name            = "auditor"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.auditor.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  depends_on = [aws_ecs_service.api]
}

# ---- load generator: not a service, started on demand with RunTask

resource "aws_ecs_task_definition" "loadgen" {
  family                   = "${local.name}-loadgen"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name             = "loadgen"
    image            = "${aws_ecr_repository.loadgen.repository_url}:${var.loadgen_image_tag}"
    essential        = true
    logConfiguration = local.logs["loadgen"]
  }])
}
