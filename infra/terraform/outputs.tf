output "server_ip" {
  value = aws_instance.server.public_ip
}

output "ssh_command" {
  value = "ssh ubuntu@${aws_instance.server.public_ip}"
}

output "next_steps" {
  value = "make ping && make deploy, then make tunnel and open http://127.0.0.1:3000"
}

output "reminder" {
  value = "This server costs money while it runs. When you are done: make tf-destroy"
}
