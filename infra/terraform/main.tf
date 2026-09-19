terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }

  # Local state: a single operator, short lived demo. A team would use the S3
  # backend with use_lockfile = true so state is shared and runs cannot overlap.
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  api_image  = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
}
