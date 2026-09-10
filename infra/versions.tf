terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags { tags = { Project = var.project, ManagedBy = "Terraform" } }
}
