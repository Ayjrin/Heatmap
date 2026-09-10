terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "aws_profile" {
  type    = string
  default = "default"
}
variable "project" {
  type    = string
  default = "proleague-heatmap"
}
provider "aws" {
  region  = var.region
  profile = var.aws_profile
}
data "aws_caller_identity" "current" {}
resource "aws_s3_bucket" "state" {
  bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}-${var.region}"
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
output "state_bucket" { value = aws_s3_bucket.state.id }
