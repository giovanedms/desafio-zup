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

# Concorrência = taxa de lotes x duração do lote. Pico: 5.000 TransacaoAutorizada/s; no pior mix (toda aprovação é
# captura, docs/capacidade.md §1.1) mais 5.000 ReservaCapturada/s = ~10.000 mensagens/s na fila contabilizacao.
# Lote de 10 com 2 escritas por evento (inbox + lançamento) a ~4 ms medidos (docs/capacidade.md §3) = ~80 ms, ~100 ms
# com o overhead da invocação. 10.000 / 10 x 0,1 s = 100 em voo; 200 dá 2x de folga para escrita lenta e cold start.
# O antigo 20 travava em 20 x 10 / 0,08 s = ~2.500 eventos/s, abaixo do pico (painel 6, D2). É teto, não reserva: não
# custa nada parado, e 200 é 20% das 1.000 execuções concorrentes da conta (ADR-0004), que o relay e o CDC dividem.
variable "concorrencia_maxima_sqs" {
  description = "Teto de concorrência do ESM da fila contabilizacao (maximum_concurrency, 2 a 1.000). 200 = ~10.000 eventos/s no pior mix com lotes de ~100 ms e 2x de folga; a conta tem 1.000 execuções concorrentes no total (ADR-0004), compartilhadas entre as funções."
  type        = number
  default     = 200

  validation {
    condition     = var.concorrencia_maxima_sqs >= 2 && var.concorrencia_maxima_sqs <= 1000
    error_message = "concorrencia_maxima_sqs deve estar entre 2 e 1000 (limites do maximum_concurrency do ESM de SQS)."
  }
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
