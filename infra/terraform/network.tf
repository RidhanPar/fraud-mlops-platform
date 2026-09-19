# A dedicated VPC across two availability zones (the load balancer and the RDS
# subnet group both require two). No NAT gateway: tasks get public IPs and are
# shielded by security groups, which saves ~$1/day. Production would put tasks
# and the database in private subnets behind NAT or VPC endpoints.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---- security groups: each tier only accepts traffic from the tier in front of it

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Load balancer: port 80 from the operator only; port 8080 for the in-VPC load test (token checked by listener rule)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "operator"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  # Traffic from a Fargate task to an internet facing ALB leaves through the
  # internet gateway, so its source is a public IP no security group can name.
  # This port is open, but its listener returns 403 unless the load test token
  # header is present.
  ingress {
    description = "load test, token gated"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "Fargate tasks: API port only from the load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "ECR, S3, CloudWatch, Secrets Manager, RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Postgres from the tasks, and from the operator for audit tooling"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  dynamic "ingress" {
    for_each = var.db_publicly_accessible ? [1] : []
    content {
      description = "operator audit tooling"
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = [var.allowed_cidr]
    }
  }
}
