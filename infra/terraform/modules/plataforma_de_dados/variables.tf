variable "account_id" {
  description = "ID da conta (sufixo do nome do bucket e condições das trust policies)."
  type        = string
}

variable "limite_de_permissoes_arn" {
  description = "ARN do permissions boundary aplicado às roles criadas aqui (vem do ambiente `esteira`)."
  type        = string
}

variable "bus_nome" {
  description = "Nome do event bus de domínio."
  type        = string
}

variable "sources" {
  description = "Valores de `source` do bus que a data platform assina (todos os detail-types de cada um). O ambiente passa module.eventos.sources; null = o default."
  type        = list(string)
  default     = ["financas.autorizacao", "financas.limites"]
  nullable    = false
}

variable "ativa" {
  description = "Regra ligada (ENABLED). false na janela de medição de carga: a data platform soma 1 invocation por evento e o Firehose Direct PUT tem quota própria. O archive guarda os eventos e o replay pode mirar só esta regra."
  type        = bool
  default     = true
}

variable "dlq_de_entrega_arn" {
  description = "ARN da DLQ de entrega dos targets do EventBridge (módulo eventos). A regra daqui precisa estar em regras_externas_na_dlq_de_entrega de lá."
  type        = string
}

variable "nome" {
  description = "Nome do Firehose (também compõe o log group e as roles)."
  type        = string
  default     = "financas-plataforma-de-dados"
}

variable "nome_consumidor" {
  description = "Nome do consumidor no bus: regra `para-<nome>`, target_id e nome do alarme."
  type        = string
  default     = "plataforma-de-dados"
}

variable "prefixo_bucket" {
  description = "Prefixo do nome do bucket; o sufixo é o account_id (nome global único)."
  type        = string
  default     = "desafio-zup-plataforma-de-dados"
}

variable "retencao_dias" {
  description = "Expiração dos objetos no bucket. Sandbox: 30 dias. Produção: Object Lock COMPLIANCE com a retenção regulatória, sem esta expiração."
  type        = number
  default     = 30
}

variable "forcar_destruicao" {
  description = "force_destroy do bucket (apaga objetos e versões no destroy). true só no sandbox."
  type        = bool
  default     = false
}

variable "buffer_segundos" {
  description = "Buffer de tempo do Firehose antes de gravar no S3 (60 a 900 s). 60 s: o objeto aparece em ~1 min na demo."
  type        = number
  default     = 60

  validation {
    condition     = var.buffer_segundos >= 60 && var.buffer_segundos <= 900
    error_message = "buffer_segundos deve estar entre 60 e 900."
  }
}

variable "buffer_mib" {
  description = "Buffer de tamanho do Firehose (MiB). Com dynamic partitioning o mínimo é 64; na demo quem dispara a gravação é o tempo."
  type        = number
  default     = 64

  validation {
    condition     = var.buffer_mib >= 64 && var.buffer_mib <= 128
    error_message = "buffer_mib deve estar entre 64 (mínimo com dynamic partitioning) e 128."
  }
}

variable "retencao_logs_dias" {
  description = "Retenção do log de erro do Firehose."
  type        = number
  default     = 7
}

variable "idade_maxima_entrega_segundos" {
  description = "Retry do target: idade máxima do evento antes da DLQ de entrega (60 a 86.400). Igual aos targets do módulo eventos."
  type        = number
  default     = 3600

  validation {
    condition     = var.idade_maxima_entrega_segundos >= 60 && var.idade_maxima_entrega_segundos <= 86400
    error_message = "idade_maxima_entrega_segundos deve estar entre 60 e 86400."
  }
}

variable "tentativas_maximas_entrega" {
  description = "Retry do target: tentativas antes da DLQ de entrega (0 a 185). Igual aos targets do módulo eventos."
  type        = number
  default     = 185

  validation {
    condition     = var.tentativas_maximas_entrega >= 0 && var.tentativas_maximas_entrega <= 185
    error_message = "tentativas_maximas_entrega deve estar entre 0 e 185."
  }
}

variable "prefixo_alarmes" {
  description = "Prefixo do nome do alarme (o mesmo dos alarmes do módulo observabilidade)."
  type        = string
  default     = "desafio-autorizacao"
}

variable "acoes_alarme" {
  description = "ARNs notificados pelo alarme (ALARM e OK): o tópico SNS de alarmes do módulo observabilidade."
  type        = list(string)
  default     = []
}

variable "defasagem_limite_segundos" {
  description = "Limiar do alarme de defasagem (DeliveryToS3.DataFreshness, máximo em 5 min)."
  type        = number
  default     = 900
}
