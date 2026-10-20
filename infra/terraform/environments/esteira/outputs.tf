output "role_de_deploy_arn" {
  description = "ARN da role que a esteira assume (segredo AWS_ROLE_ARN do repositório)."
  value       = aws_iam_role.esteira.arn
}

output "limite_de_permissoes_arn" {
  description = "ARN do permissions boundary das roles da stack."
  value       = aws_iam_policy.limite.arn
}
