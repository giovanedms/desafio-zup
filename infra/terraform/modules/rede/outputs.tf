output "vpc_id" {
  description = "ID da VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "CIDR da VPC."
  value       = aws_vpc.this.cidr_block
}

output "subnet_ids_publicas" {
  description = "IDs das subnets públicas (uma por AZ)."
  value       = aws_subnet.publica[*].id
}

output "sg_tasks_id" {
  description = "ID do SG das tasks Fargate."
  value       = aws_security_group.tasks.id
}
