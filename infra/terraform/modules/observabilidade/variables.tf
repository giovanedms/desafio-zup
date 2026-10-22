variable "nome" {
  description = "Prefixo do dashboard e dos alarmes."
  type        = string
  default     = "desafio-autorizacao"
}

variable "regiao" {
  description = "Região AWS dos widgets."
  type        = string
  default     = "sa-east-1"
}

variable "api_nome" {
  description = "Nome da REST API (dimensão ApiName)."
  type        = string
}

variable "api_stage" {
  description = "Nome do stage (dimensão Stage)."
  type        = string
  default     = "v1"
}

variable "tabelas" {
  description = "Nomes das tabelas DynamoDB exibidas no dashboard."
  type        = list(string)
}

variable "tabelas_com_latencia" {
  description = "Tabelas do caminho síncrono cujo SuccessfulRequestLatency é exibido."
  type        = list(string)
}

variable "cluster_nome" {
  description = "Nome do cluster ECS."
  type        = string
}

variable "servicos_ecs" {
  description = "Nomes dos services ECS."
  type        = list(string)
}

variable "filas" {
  description = "Nomes das filas de consumidores."
  type        = list(string)
}

variable "dlqs" {
  description = "Nomes de TODAS as DLQs (filas + relay-dlq); cada uma ganha alarme."
  type        = list(string)
}

variable "bus_nome" {
  description = "Nome do event bus de domínio (dimensão EventBusName dos widgets do EventBridge)."
  type        = string
}

variable "regras_do_bus" {
  description = "Nomes das regras do bus exibidas no dashboard (Invocations e FailedInvocations por regra): as das filas consumidoras, a do LimiteAlterado e a da data platform."
  type        = list(string)
}

variable "funcoes_consumidoras" {
  description = "Funções Lambda que consomem as filas (contabilização e consumidores de referência): invocações e erros no dashboard."
  type        = list(string)
}

variable "firehose_nome" {
  description = "Nome do Firehose da data platform (DeliveryToS3.Records e DeliveryToS3.DataFreshness no dashboard)."
  type        = string
}

variable "relay_funcao" {
  description = "Nome da função relay."
  type        = string
}

variable "account_id" {
  description = "ID da conta AWS (política da chave KMS e condição anti-confused-deputy do tópico de alarmes)."
  type        = string
}

variable "acoes_alarme" {
  description = "ARNs extras notificados pelos alarmes, além do tópico SNS de alarmes criado aqui (ex.: tópico do plantão central)."
  type        = list(string)
  default     = []
}

variable "email_alarmes" {
  description = "E-mail assinado no tópico de alarmes. Vazio (padrão) = sem assinatura; o tópico existe e recebe os alarmes."
  type        = string
  default     = ""
}

variable "fila_legado" {
  description = "Nome da fila do feed do core legado (alarme de defasagem, ADR-0014)."
  type        = string
}

variable "defasagem_legado_limite_s" {
  description = "Limiar do alarme de defasagem do legado: idade da mensagem mais antiga da fila, em segundos (ADR-0014: 5 min)."
  type        = number
  default     = 300
}

variable "limite_reservas_ativas" {
  description = "Limiar do alarme de reservas ativas no item de um contrato (~2.000 x ~45 bytes = ~90 KB; o item vai até 400 KB)."
  type        = number
  default     = 2000
}

variable "p99_limite_ms" {
  description = "SLO de latência p99 no gateway (ADR-0001: 150 ms). Linha do dashboard; NÃO é o limiar do alarme (p99_alarme_ms)."
  type        = number
  default     = 150
}

variable "p99_alarme_ms" {
  description = "Limiar do alarme de latência p99 (3 de 5 min). 400 ms, ~2,7x o SLO: acima do medido com folga de CPU (164 ms) e abaixo da saturação (874 ms); alarmar no SLO dispararia sempre, porque o entregue não o atinge (docs/capacidade.md, seção 3)."
  type        = number
  default     = 400
}

variable "slo_disponibilidade" {
  description = "SLO de disponibilidade mensal da API (ADR-0001: 99,9%). Base do burn rate dos alarmes de 5XX (orçamento = 1 - SLO)."
  type        = number
  default     = 0.999

  validation {
    condition     = var.slo_disponibilidade > 0 && var.slo_disponibilidade < 1
    error_message = "slo_disponibilidade deve estar entre 0 e 1 (ex.: 0.999)."
  }
}

variable "burn_rate_rps_minimo" {
  description = "Média mínima de requisições por segundo numa janela para o burn rate avaliar (abaixo disso a janela conta como OK: sem amostra para medir 0,1%)."
  type        = number
  default     = 1
}

variable "idade_fila_limite_s" {
  description = "Limiar do alarme de idade da mensagem mais antiga nas filas consumidoras (s). 300 > visibilidade de 180 s: uma única falha transitória (a mensagem volta depois da visibilidade) não dispara."
  type        = number
  default     = 300
}

variable "funcoes_lambda" {
  description = "Chave curta -> nome da função Lambda. Cada uma ganha alarme de Errors >= 1 em 5 min; o log group de relay e sweeper (/aws/lambda/<função>) recebe o metric filter de outbox_registro_malformado."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["relay", "sweeper"] : contains(keys(var.funcoes_lambda), k)])
    error_message = "funcoes_lambda precisa das chaves relay e sweeper (metric filter de item de outbox malformado)."
  }
}

variable "cdc_funcao" {
  description = "Nome da função relay-cdc-limites (alarme de IteratorAge, como o do relay)."
  type        = string
}

variable "log_group_autorizacao" {
  description = "Nome do log group do serviço autorizacao (origem da linha motivo_desconhecido_do_limite)."
  type        = string
}

variable "log_group_cdc" {
  description = "Nome do log group da função relay-cdc-limites (origem da linha cdc_registro_malformado)."
  type        = string
}

variable "iterator_age_limite_ms" {
  description = "Limiar do IteratorAge do relay e do relay-cdc-limites (60 s)."
  type        = number
  default     = 60000
}

variable "log_group_limites" {
  description = "Nome do log group do serviço limites (origem do evento guarda_de_idempotencia)."
  type        = string
}

variable "log_group_reconciliador" {
  description = "Nome do log group do reconciliador (linha de rodada com as contagens de órfãs e reservas vencidas)."
  type        = string
}

variable "limite_guarda_operacoes" {
  description = "Limiar do alarme de tamanho da guarda de idempotência de um contrato (operações na guarda)."
  type        = number
  default     = 2000
}
