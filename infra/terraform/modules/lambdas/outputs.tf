output "relay_nome" {
  description = "Nome da função relay."
  value       = aws_lambda_function.relay.function_name
}

output "relay_dlq_arn" {
  description = "ARN da DLQ do relay."
  value       = aws_sqs_queue.relay_dlq.arn
}

output "relay_dlq_nome" {
  description = "Nome da DLQ do relay."
  value       = aws_sqs_queue.relay_dlq.name
}

output "contabilizacao_nome" {
  description = "Nome da função de contabilização."
  value       = aws_lambda_function.contabilizacao.function_name
}

output "sweeper_nome" {
  description = "Nome da função sweeper."
  value       = aws_lambda_function.sweeper.function_name
}

output "cdc_limites_nome" {
  description = "Nome da função relay-cdc-limites."
  value       = aws_lambda_function.cdc_limites.function_name
}

output "cdc_limites_dlq_nome" {
  description = "Nome da DLQ do relay-cdc-limites."
  value       = aws_sqs_queue.cdc_limites_dlq.name
}

output "cdc_limites_log_group_nome" {
  description = "Log group da função relay-cdc-limites (metric filter de registro malformado)."
  value       = aws_cloudwatch_log_group.this["cdc_limites"].name
}
