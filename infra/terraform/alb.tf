resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 30
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 15

  health_check {
    path                = "/ready"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
  }
}

# Port 80: the operator (security group allows only allowed_cidr).
# Plain HTTP because there is no domain for a TLS certificate; see the README scope section.
resource "aws_lb_listener" "operator" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# Port 8080: the in-VPC load generator. Everything is refused unless the
# request carries the load test token.
resource "aws_lb_listener" "loadtest" {
  load_balancer_arn = aws_lb.main.arn
  port              = 8080
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "loadtest_token" {
  listener_arn = aws_lb_listener.loadtest.arn
  priority     = 1

  condition {
    http_header {
      http_header_name = "X-Loadtest-Token"
      values           = [random_password.loadtest_token.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
