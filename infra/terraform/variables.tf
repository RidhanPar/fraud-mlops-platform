variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "aws_profile" {
  description = "Named profile in ~/.aws/credentials for the least privilege deployer"
  type        = string
  default     = "fraud-mlops"
}

variable "project" {
  type    = string
  default = "fraud-mlops"
}

variable "allowed_cidr" {
  description = "The only network allowed to reach the load balancer and the database, e.g. 203.0.113.7/32"
  type        = string

  validation {
    condition     = can(cidrhost(var.allowed_cidr, 0)) && var.allowed_cidr != "0.0.0.0/0"
    error_message = "Use a specific CIDR such as your own IP with /32, never 0.0.0.0/0."
  }
}

variable "image_tag" {
  description = "Tag of the fraud-detector image in ECR (the git commit it was built from)"
  type        = string
}

variable "loadgen_image_tag" {
  type    = string
  default = "latest"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "db_publicly_accessible" {
  description = "Lets audit tooling on the operator's machine reach RDS directly, still limited to allowed_cidr. Production would use a bastion or SSM port forwarding instead."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Optional address subscribed to the alarm topic (AWS sends a confirmation email)"
  type        = string
  default     = ""
}


variable "db_instance_class" {
  description = "db.t4g.micro (1 GB) swapped under load in testing; see docs/AWS.md"
  type        = string
  default     = "db.t4g.micro"
}
