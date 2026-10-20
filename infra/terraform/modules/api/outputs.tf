output "invoke_url" {
  description = "URL base do stage (https://<id>.execute-api.<regiao>.amazonaws.com/v1)."
  value       = aws_api_gateway_stage.this.invoke_url
}

output "api_nome" {
  description = "Nome da REST API (dimensão ApiName das métricas)."
  value       = aws_api_gateway_rest_api.this.name
}

output "stage_nome" {
  description = "Nome do stage."
  value       = aws_api_gateway_stage.this.stage_name
}

output "stage_arn" {
  description = "ARN do stage."
  value       = aws_api_gateway_stage.this.arn
}

output "nlb_dns" {
  description = "DNS do NLB interno."
  value       = aws_lb.this.dns_name
}

output "target_group_arn" {
  description = "ARN do target group; só fica disponível depois que o listener existe (o ECS exige isso)."
  value       = aws_lb_target_group.autorizacao.arn

  depends_on = [aws_lb_listener.http]
}

output "token_endpoint" {
  description = "Endpoint OAuth2 token do Cognito."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${data.aws_region.atual.region}.amazoncognito.com/oauth2/token"
}

output "cognito_client_ids" {
  description = "Canal -> client_id."
  value       = { for k, c in aws_cognito_user_pool_client.canal : k => c.id }
}

output "cognito_client_secrets" {
  description = "Canal -> client_secret (sensível)."
  value       = { for k, c in aws_cognito_user_pool_client.canal : k => c.client_secret }
  sensitive   = true
}

output "api_keys" {
  description = "Canal -> valor da API key (sensível)."
  value       = { for k, a in aws_api_gateway_api_key.canal : k => a.value }
  sensitive   = true
}

output "respostas_do_gateway" {
  description = "Gateway Responses injetadas no body (problem+json), para teste e inspeção."
  value       = local.respostas_do_gateway
}

output "validadores" {
  description = "Validadores de requisição injetados no body (o corpo fica com o serviço), para teste e inspeção."
  value       = local.especificacao["x-amazon-apigateway-request-validators"]
}

output "parametros_da_integracao" {
  description = "Caminho -> método -> requestParameters da integração (headers injetados pelo gateway), para teste."
  value = {
    for caminho, operacoes in local.paths : caminho => {
      for metodo, op in operacoes : metodo => op["x-amazon-apigateway-integration"].requestParameters
    }
  }
}
