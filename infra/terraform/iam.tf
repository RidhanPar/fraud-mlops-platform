data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---- execution role: used by ECS itself to pull images, write logs, inject secrets

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-db-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [local.admin_secret_arn, aws_secretsmanager_secret.app_db.arn]
    }]
  })
}

# ---- task roles: what the code inside each container may call. The API has none.

resource "aws_iam_role" "monitor" {
  name               = "${local.name}-monitor"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "monitor" {
  name = "publish-drift-metrics"
  role = aws_iam_role.monitor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["cloudwatch:PutMetricData"]
      Resource  = "*"
      Condition = { StringEquals = { "cloudwatch:namespace" = "FraudMLOps" } }
    }]
  })
}

resource "aws_iam_role" "auditor" {
  name               = "${local.name}-auditor"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "auditor" {
  name = "anchor-seals"
  role = aws_iam_role.auditor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Write and read seal copies; no delete, no retention changes, no bypass.
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.seals.arn}/seals/*"
      },
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.seals.arn
        Condition = { StringLike = { "s3:prefix" = ["seals/*"] } }
      },
    ]
  })
}

# ---- CI: GitHub Actions pushes images through OIDC, no long lived keys

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.github_repo == "" ? 0 : 1
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_ci" {
  count = var.github_repo == "" ? 0 : 1
  name  = "${local.name}-github-ci"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        # Only pushes to main in this one repository can assume the role.
        StringLike = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_ci" {
  count = var.github_repo == "" ? 0 : 1
  name  = "push-fraud-detector-image"
  role  = aws_iam_role.github_ci[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages",
        ]
        Resource = aws_ecr_repository.api.arn
      },
    ]
  })
}
