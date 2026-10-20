output "bus_nome" {
  description = "Nome do bus."
  value       = aws_cloudwatch_event_bus.this.name
}

output "bus_arn" {
  description = "ARN do bus."
  value       = aws_cloudwatch_event_bus.this.arn
}

output "filas_arns" {
  description = "Consumidor -> ARN da fila."
  value       = { for k, q in aws_sqs_queue.this : k => q.arn }
}

output "filas_nomes" {
  description = "Consumidor -> nome da fila."
  value       = { for k, q in aws_sqs_queue.this : k => q.name }
}

output "filas_urls" {
  description = "Consumidor -> URL da fila."
  value       = { for k, q in aws_sqs_queue.this : k => q.url }
}

output "dlqs_arns" {
  description = "Consumidor -> ARN da DLQ."
  value       = { for k, q in aws_sqs_queue.dlq : k => q.arn }
}

output "dlqs_nomes" {
  description = "Consumidor -> nome da DLQ."
  value       = { for k, q in aws_sqs_queue.dlq : k => q.name }
}

output "regras_arns" {
  description = "Consumidor -> ARN da regra."
  value       = { for k, r in aws_cloudwatch_event_rule.this : k => r.arn }
}

output "fila_legado_arn" {
  description = "ARN da fila do feed do legado."
  value       = aws_sqs_queue.legado.arn
}

output "fila_legado_nome" {
  description = "Nome da fila do feed do legado."
  value       = aws_sqs_queue.legado.name
}

output "fila_legado_url" {
  description = "URL da fila do feed do legado."
  value       = aws_sqs_queue.legado.url
}

output "fila_legado_dlq_nome" {
  description = "Nome da DLQ do feed do legado."
  value       = aws_sqs_queue.legado_dlq.name
}
