output "urls" {
  description = "Mapa nome curto -> URL do repositório."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "arns" {
  description = "Mapa nome curto -> ARN do repositório."
  value       = { for k, r in aws_ecr_repository.this : k => r.arn }
}
