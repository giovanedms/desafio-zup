output "invoke_url" {
  description = "URL base da API (stage v1)."
  value       = module.api.invoke_url
}

output "token_endpoint" {
  description = "Endpoint OAuth2 do Cognito."
  value       = module.api.token_endpoint
}

output "cognito_client_ids" {
  description = "Canal -> client_id."
  value       = module.api.cognito_client_ids
}

output "cognito_client_secrets" {
  description = "Canal -> client_secret (sensível: `terraform output -json cognito_client_secrets`)."
  value       = module.api.cognito_client_secrets
  sensitive   = true
}

output "api_keys" {
  description = "Canal -> API key (sensível: `terraform output -json api_keys`)."
  value       = module.api.api_keys
  sensitive   = true
}

output "ecr_urls" {
  description = "Repositórios ECR (push das imagens)."
  value       = module.ecr.urls
}

output "dashboard" {
  description = "Dashboard CloudWatch."
  value       = module.observabilidade.dashboard_nome
}
