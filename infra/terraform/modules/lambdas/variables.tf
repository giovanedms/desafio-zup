variable "prefixo" {
  description = "Prefixo dos nomes das funções."
  type        = string
  default     = "desafio"
}

variable "zip_relay" {
  description = "Caminho do zip do pacote relay (gerado por scripts/empacotar_lambdas.sh)."
  type        = string
}

variable "zip_contabilizacao" {
  description = "Caminho do zip do pacote contabilizacao."
  type        = string
}

variable "bus_nome" {
  description = "Nome do event bus."
  type        = string
}

variable "bus_arn" {
  description = "ARN do event bus."
  type        = string
}

variable "outbox_tabela_nome" {
  description = "Nome da tabela outbox."
  type        = string
}

variable "outbox_tabela_arn" {
  description = "ARN da tabela outbox."
  type        = string
}

variable "outbox_stream_arn" {
  description = "ARN do stream da tabela outbox."
  type        = string
}

variable "limites_stream_arn" {
  description = "ARN do stream da tabela limites (NEW_AND_OLD_IMAGES), lido pelo relay-cdc-limites."
  type        = string
}

variable "fila_contabilizacao_arn" {
  description = "ARN da fila contabilizacao."
  type        = string
}

variable "contabilizacao_tabelas" {
  description = "Tabelas da contabilização: lancamentos e inbox (nome e ARN)."
  type = object({
    lancamentos_nome = string
    lancamentos_arn  = string
    inbox_nome       = string
    inbox_arn        = string
  })
}

variable "retencao_logs_dias" {
  description = "Retenção dos log groups."
  type        = number
  default     = 7
}

variable "concorrencia_maxima_sqs" {
  description = "Concorrência máxima do ESM da fila contabilizacao (a conta nova tem 400 no total, ADR-0004)."
  type        = number
  default     = 20
}

variable "memoria_mb" {
  description = "Memória das funções."
  type        = number
  default     = 256
}

variable "timeout_segundos" {
  description = "Timeout das funções (menor que o visibility timeout da fila)."
  type        = number
  default     = 30
}

variable "limite_de_permissoes_arn" {
  description = "ARN do permissions boundary aplicado a toda role criada aqui (teto de permissões; vem do ambiente `esteira`)."
  type        = string
}

variable "account_id" {
  description = "ID da conta AWS (condição anti-confused-deputy da trust policy da role do scheduler)."
  type        = string
}

variable "regiao" {
  description = "Região AWS (ARN do schedule group na trust policy da role do scheduler)."
  type        = string
  default     = "sa-east-1"
}
