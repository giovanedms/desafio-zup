variable "nome" {
  description = "Prefixo dos recursos da API."
  type        = string
  default     = "desafio-autorizacao"
}

variable "account_id" {
  description = "ID da conta AWS (sufixo do domínio Cognito, que é global na região)."
  type        = string
}

variable "vpc_id" {
  description = "VPC do NLB."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets do NLB interno."
  type        = list(string)
}

variable "contrato_openapi" {
  description = "Caminho do contrato OpenAPI 3.0 (contract-first). O Terraform injeta as extensões x-amazon-apigateway-* sem editar o arquivo."
  type        = string
}

variable "porta_alvo" {
  description = "Porta do serviço autorizacao."
  type        = number
  default     = 8080
}

variable "caminho_saude" {
  description = "Health check do target group."
  type        = string
  default     = "/saude"
}

variable "nome_stage" {
  description = "Nome do stage."
  type        = string
  default     = "v1"
}

variable "retencao_logs_dias" {
  description = "Retenção do log group de access log."
  type        = number
  default     = 7
}

variable "waf_limite_por_ip" {
  description = "Requisições por IP em 5 minutos antes do bloqueio pelo WAF."
  type        = number
  default     = 2000
}

variable "deletion_protection" {
  description = "Proteção contra exclusão do NLB. false no sandbox; true em produção."
  type        = bool
  default     = false
}

variable "planos" {
  description = "Usage plans por nome."
  type = map(object({
    rate_limit  = number
    burst_limit = number
    quota_dia   = number
  }))
  default = {
    "plano-padrao" = { rate_limit = 200, burst_limit = 400, quota_dia = 1000000 }
    # Limites baixos de propósito: demonstram o 429 (THROTTLED/QUOTA_EXCEEDED) em problem+json.
    "plano-restrito" = { rate_limit = 5, burst_limit = 10, quota_dia = 1000 }
    # Só para a janela de medição (k6): não é um plano de canal de produção.
    "plano-carga" = { rate_limit = 6000, burst_limit = 5000, quota_dia = 50000000 }
  }
}

variable "canais" {
  description = "Canal -> nome do usage plan. Cada canal ganha um app client Cognito e uma API key."
  type        = map(string)
  default = {
    "canal-pos"   = "plano-padrao"
    "canal-app"   = "plano-restrito"
    "canal-carga" = "plano-carga"
  }
}

variable "limite_de_permissoes_arn" {
  description = "ARN do permissions boundary aplicado a toda role criada aqui (teto de permissões; vem do ambiente `esteira`)."
  type        = string
}
