# Reused from eth-node-iac.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

# Credentials are NOT set here. Terraform reads them from your AWS CLI profile
# or AWS_* environment variables. Check with: aws sts get-caller-identity
provider "aws" {
  region = var.aws_region
}
