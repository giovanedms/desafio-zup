variable "prefixo" {
  description = "Prefixo opcional dos nomes das tabelas (vazio = nomes idênticos aos do ambiente local)."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Proteção contra exclusão das tabelas. false no sandbox para o `make destroy` funcionar; true em produção."
  type        = bool
  default     = false
}
