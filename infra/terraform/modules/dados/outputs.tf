output "nomes" {
  description = "Mapa chave lógica -> nome da tabela."
  value       = { for k, t in aws_dynamodb_table.this : k => t.name }
}

output "arns" {
  description = "Mapa chave lógica -> ARN da tabela."
  value       = { for k, t in aws_dynamodb_table.this : k => t.arn }
}

output "stream_arns" {
  description = "ARN dos streams (somente tabelas com stream: limites e autorizacoes-outbox)."
  value       = { for k, t in aws_dynamodb_table.this : k => t.stream_arn if t.stream_enabled }
}
