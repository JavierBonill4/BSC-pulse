variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ssh_public_key_path" {
  description = "Path to your SSH public key"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "server_name" {
  type    = string
  default = "bsc-pulse"
}

# Which instance types are free depends on your AWS plan. t3.small (2 GB RAM) fits
# the watchdog, notifier, Prometheus, Alertmanager and Grafana. t3.micro (1 GB) is tight.
variable "instance_type" {
  type    = string
  default = "t3.small"
}

# Must match the instance type: "amd64" for t3, "arm64" for t4g.
variable "architecture" {
  type    = string
  default = "amd64"
  validation {
    condition     = contains(["amd64", "arm64"], var.architecture)
    error_message = "architecture must be amd64 or arm64."
  }
}

variable "root_volume_gb" {
  description = "Disk size. Docker images plus 15 days of Prometheus data fit in 20 GB."
  type        = number
  default     = 20
}

# Changed from eth-node-iac: no default, and the whole internet is refused.
# SSH is the only open port, so it must be limited to your own IP.
variable "allowed_ssh_cidrs" {
  description = "Who may SSH in: your IP as a /32, e.g. [\"203.0.113.5/32\"]. Find it: curl https://checkip.amazonaws.com"
  type        = list(string)
  validation {
    condition     = length(var.allowed_ssh_cidrs) > 0 && alltrue([for cidr in var.allowed_ssh_cidrs : can(cidrhost(cidr, 0)) && cidr != "0.0.0.0/0"])
    error_message = "Set allowed_ssh_cidrs to your own IP, e.g. [\"203.0.113.5/32\"]. 0.0.0.0/0 is not allowed."
  }
}
