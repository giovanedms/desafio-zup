output "nome" {
  description = "Nome do service ECS."
  value       = aws_ecs_service.this.name
}

output "log_group_nome" {
  description = "Nome do log group."
  value       = aws_cloudwatch_log_group.this.name
}

output "execucao_role_arn" {
  description = "ARN da execution role."
  value       = aws_iam_role.execucao.arn
}
