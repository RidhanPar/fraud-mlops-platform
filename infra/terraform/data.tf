# ---- RDS Postgres: the prediction log and audit trail

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "fraud"
  username = "fraud_admin"
  # RDS generates the admin password and keeps it in Secrets Manager. It never
  # appears in Terraform state, code or task definitions.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = var.db_publicly_accessible

  backup_retention_period    = 1
  auto_minor_version_upgrade = true
  apply_immediately          = true

  # Demo settings so teardown is one command. Production: deletion_protection = true,
  # a final snapshot, Multi-AZ, longer backups.
  deletion_protection = false
  skip_final_snapshot = true
  multi_az            = false
}

locals {
  admin_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn
}

# ---- the application role's password (INSERT and SELECT only, created by the migration)

resource "random_password" "app_db" {
  length  = 32
  special = false # the migration only accepts [A-Za-z0-9_]
}

resource "aws_secretsmanager_secret" "app_db" {
  name                    = "${local.name}/app-db-password"
  recovery_window_in_days = 0 # demo: allow immediate re-create after destroy
}

resource "aws_secretsmanager_secret_version" "app_db" {
  secret_id     = aws_secretsmanager_secret.app_db.id
  secret_string = random_password.app_db.result
}

# ---- load test token for the token gated listener

resource "random_password" "loadtest_token" {
  length  = 40
  special = false
}

# ---- write once copies of audit seals (S3 Object Lock)

resource "aws_s3_bucket" "seals" {
  bucket              = "${local.name}-seals-${local.account_id}"
  object_lock_enabled = true
  force_destroy       = true
}

resource "aws_s3_bucket_versioning" "seals" {
  bucket = aws_s3_bucket.seals.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "seals" {
  bucket = aws_s3_bucket.seals.id

  rule {
    default_retention {
      # GOVERNANCE: nobody can change or delete a seal copy for a day unless they
      # hold s3:BypassGovernanceRetention, which only the deployer has (for
      # teardown). COMPLIANCE mode would bind even the root user, and would block
      # deleting this demo bucket until retention expired.
      mode = "GOVERNANCE"
      days = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.seals]
}

resource "aws_s3_bucket_public_access_block" "seals" {
  bucket                  = aws_s3_bucket.seals.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "seals" {
  bucket = aws_s3_bucket.seals.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- container registry

resource "aws_ecr_repository" "api" {
  name = "${local.name}/fraud-detector"
  # Tags are git commits and can never be overwritten, so a tag always means
  # the same bytes (the audit log relies on that too).
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "loadgen" {
  name                 = "${local.name}/loadgen"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
