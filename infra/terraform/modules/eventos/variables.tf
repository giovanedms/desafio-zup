variable "nome_bus" {
  description = "Nome do event bus."
  type        = string
  default     = "financas"
}

variable "source_eventos" {
  description = "Valor de `source` publicado pelo relay (igual ao bootstrap local)."
  type        = string
  default     = "financas.autorizacao"
}

variable "assinaturas" {
  description = "Consumidor -> detail-types que ele assina (IGUAL a ASSINATURAS do infra/local/bootstrap.py)."
  type        = map(list(string))
  default = {
    contabilizacao = ["TransacaoAutorizada", "TransacaoEstornada", "ReservaExpirada", "ReservaCapturada"]
    antifraude     = ["TransacaoAutorizada", "TransacaoRecusada"]
    notificacoes   = ["TransacaoAutorizada", "TransacaoRecusada", "TransacaoEstornada"]
  }
}

variable "retencao_archive_dias" {
  description = "Retenção do archive do bus. Sandbox: 30 dias."
  type        = number
  default     = 30
}

variable "visibilidade_segundos" {
  description = "Visibility timeout das filas (>= timeout do consumidor; recomendado 6x)."
  type        = number
  default     = 180
}

variable "max_receive_count" {
  description = "Tentativas antes de ir para a DLQ."
  type        = number
  default     = 5
}

variable "fila_legado" {
  description = "Nome da fila do feed do core legado (IGUAL a FILA_DO_LEGADO do infra/local/bootstrap.py)."
  type        = string
  default     = "legado-contratos"
}

variable "source_limites" {
  description = "Valor de `source` do LimiteAlterado publicado pelo CDC da tabela limites (igual ao bootstrap local)."
  type        = string
  default     = "financas.limites"
}

variable "consumidor_limite_alterado" {
  description = "Consumidor (chave de `assinaturas`) que recebe LimiteAlterado."
  type        = string
  default     = "notificacoes"
}

variable "idade_maxima_entrega_segundos" {
  description = "Retry dos targets: idade máxima do evento antes de ir para a DLQ de entrega (60 a 86.400). 1 h cobre falha transitória do SQS; depois disso o evento vai para a DLQ, com alarme, em vez de 24 h de tentativas silenciosas."
  type        = number
  default     = 3600

  validation {
    condition     = var.idade_maxima_entrega_segundos >= 60 && var.idade_maxima_entrega_segundos <= 86400
    error_message = "idade_maxima_entrega_segundos deve estar entre 60 e 86400."
  }
}

variable "tentativas_maximas_entrega" {
  description = "Retry dos targets: tentativas antes da DLQ de entrega (0 a 185). Com 185, quem limita é a idade máxima."
  type        = number
  default     = 185

  validation {
    condition     = var.tentativas_maximas_entrega >= 0 && var.tentativas_maximas_entrega <= 185
    error_message = "tentativas_maximas_entrega deve estar entre 0 e 185."
  }
}

variable "regras_externas_na_dlq_de_entrega" {
  description = "ARNs de regras deste bus criadas fora do módulo (ex.: a da data platform) cujos targets usam a DLQ de entrega. Entram no aws:SourceArn da policy da DLQ."
  type        = list(string)
  default     = []
}

variable "diretorio_contratos" {
  description = "Diretório dos contratos de evento (*.schema.json) publicados no schema registry. null = contracts/events do repositório."
  type        = string
  default     = null
}
