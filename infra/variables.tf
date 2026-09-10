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
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,28}$", var.project))
    error_message = "Use 3–29 lowercase letters, digits, and hyphens."
  }
}
variable "image_tag" {
  type        = string
  description = "Immutable ECR tag built from this checkout. Push it before launching collection."
  default     = "bootstrap"
}
variable "ssm_parameter_name" {
  type        = string
  default     = "/proleague-heatmap/riot-api-key"
  description = "Existing SecureString name only. Set its value outside Terraform."
}
variable "ssm_kms_key_arn" {
  type        = string
  default     = null
  description = "Optional customer-managed key for the existing SSM parameter."
}
variable "task_architecture" {
  type    = string
  default = "ARM64"
  validation {
    condition     = contains(["ARM64", "X86_64"], var.task_architecture)
    error_message = "Choose ARM64 (Docker linux/arm64) or X86_64 (Docker linux/amd64)."
  }
}
