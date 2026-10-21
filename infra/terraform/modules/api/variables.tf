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

variable "waf_limite_por_consumidor" {
  description = "Requisições por x-api-key em 5 minutos antes do 429 do WAF. Padrão: 2x o maior plano de canal (plano-padrao, 200 rps = 60.000/5 min), teto de proteção acima do usage plan. A janela de medição com o plano-carga precisa subir este valor."
  type        = number
  default     = 120000
}

variable "waf_limite_por_ip" {
  description = "Requisições por IP em 5 minutos antes do 429 do WAF. Padrão: 1.000 rps (10% do throttle de 10.000 rps da conta), acima do teto por consumidor para não barrar um egress (NAT) que carregue vários canais."
  type        = number
  default     = 300000
}

variable "deletion_protection" {
  description = "Proteção contra exclusão do NLB. false no sandbox; true em produção."
  type        = bool
  default     = false
}

variable "planos" {
  description = "Usage plans dos canais por nome (o do canal-carga fica em var.plano_carga)."
  type = map(object({
    rate_limit  = number
    burst_limit = number
    quota_dia   = number
  }))
  default = {
    "plano-padrao" = { rate_limit = 200, burst_limit = 400, quota_dia = 1000000 }
    # Limites baixos de propósito: demonstram o 429 (THROTTLED/QUOTA_EXCEEDED) em problem+json.
    "plano-restrito" = { rate_limit = 5, burst_limit = 10, quota_dia = 1000 }
  }
}

variable "plano_carga" {
  description = "Plano do canal-carga (só a janela de medição com k6; não é plano de canal de produção). Padrão estacionado no nível do plano-padrao: 200 rps, metade do teto do WAF por consumidor (waf_limite_por_consumidor = 120.000/5 min = 400 rps), para um apply limpo não criar uma API key com 60% do throttle da conta. A medição sobe este plano E os dois tetos do WAF pelo tfvars (terraform.tfvars.example)."
  type = object({
    rate_limit  = number
    burst_limit = number
    quota_dia   = number
  })
  default = { rate_limit = 200, burst_limit = 400, quota_dia = 1000000 }
}

# Orçamento do caminho síncrono, com os timeouts do código:
#   DynamoDB do autorizacao (2 chamadas: registro e TransactWriteItems): read_timeout 2 s, e retries max_attempts 3, que
#     no botocore são 3 NOVAS tentativas, 4 no total (botocore/config.py) -> até 8 s por chamada, mais até ~7 s de
#     backoff (services/autorizacao/src/autorizacao/main.py);
#   limites: 2 tentativas x 2 s (PRAZO_POR_TENTATIVA_S, limites_http.py) = ~4 s.
# 10 s cobrem a falha transitória que as tentativas existem para absorver: um timeout no limites com a nova tentativa
# (~4 s) ou 2-3 timeouts seguidos numa chamada ao DynamoDB (~4-7 s com o backoff), mais o resto do caminho (dezenas de
# ms). O pior caso teórico (todas as tentativas nas 3 chamadas, ~34 s) nem cabia nos 29 s de antes; nesse regime o canal
# ganha mais recebendo o 504 TEMPO_ESGOTADO (problem+json; catálogo: "o resultado pode ter sido aplicado") e repetindo
# com a MESMA Idempotency-Key, ou fazendo GET, do que esperando. A idempotência torna o corte seguro: a autorização que
# terminar depois do 504 responde no replay. É teto de proteção, não SLO (p99 alvo 150 ms, ADR-0001).
variable "timeout_integracao_ms" {
  description = "Timeout da integração API Gateway -> NLB (ms). 10.000: cobre a falha transitória que as tentativas do código absorvem (conta no comentário acima); acima disso o canal recebe 504 TEMPO_ESGOTADO e repete com a mesma Idempotency-Key. Faixa da AWS: 50 a 29.000."
  type        = number
  default     = 10000

  validation {
    condition     = var.timeout_integracao_ms >= 50 && var.timeout_integracao_ms <= 29000
    error_message = "timeout_integracao_ms deve estar entre 50 e 29000 (limites do API Gateway REST)."
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
