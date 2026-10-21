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

output "sg_ids" {
  description = "Serviço -> ID do SG das tasks dele (autorizacao, limites, limites-acl, reconciliador)."
  value       = local.sgs_dos_servicos
}
