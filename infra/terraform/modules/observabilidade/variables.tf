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
  description = "Limiar do alarme de latência p99 (RNF: 150 ms)."
  type        = number
  default     = 150
}

variable "iterator_age_limite_ms" {
  description = "Limiar do IteratorAge do relay (60 s)."
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
