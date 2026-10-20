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
