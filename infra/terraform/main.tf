# Adapted from eth-node-iac: same server recipe, minus the Ethereum P2P ports.

# Latest Canonical Ubuntu 24.04 AMI, looked up through AWS's public SSM parameter.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/${var.architecture}/hvm/ebs-gp3/ami-id"
}

# Uses your account's default VPC (every new account has one).
data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "default" {
  key_name   = "${var.server_name}-key"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# The only way in is SSH from allowed_ssh_cidrs. Grafana, Prometheus and the rest
# listen on 127.0.0.1 on the server and are reached through an SSH tunnel.
resource "aws_security_group" "server" {
  name        = "${var.server_name}-sg"
  description = "SSH only, from allowed_ssh_cidrs"
  vpc_id      = data.aws_vpc.default.id
}

# Outbound is open: the watchdog calls public RPC endpoints, the notifier calls
# Telegram, and Docker pulls images.
resource "aws_vpc_security_group_egress_rule" "all_out" {
  security_group_id = aws_security_group.server.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each          = toset(var.allowed_ssh_cidrs)
  security_group_id = aws_security_group.server.id
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = each.value
}

resource "aws_instance" "server" {
  ami                         = data.aws_ssm_parameter.ubuntu.value
  instance_type               = var.instance_type
  key_name                    = aws_key_pair.default.key_name
  vpc_security_group_ids      = [aws_security_group.server.id]
  associate_public_ip_address = true

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only (security best practice)
  }

  tags = {
    Name    = var.server_name
    project = "bsc-pulse"
  }
}

# Generate the Ansible inventory from Terraform's output so the two tools stay in sync.
# The Ubuntu AMI already has an "ubuntu" user with sudo, so no bootstrap step is needed.
resource "local_file" "ansible_inventory" {
  filename        = "${path.module}/../ansible/inventory/hosts.ini"
  file_permission = "0644"
  content         = <<-EOT
    [bsc_pulse]
    ${var.server_name} ansible_host=${aws_instance.server.public_ip} ansible_user=ubuntu
  EOT
}
