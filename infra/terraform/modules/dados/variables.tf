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

variable "warm_throughput" {
  description = <<-EOT
    Warm throughput (pré-aquecimento) por tabela e por GSI, em unidades por segundo. Vazio (padrão) = desligado.
    Aumentar é cobrado e NÃO pode ser desfeito (a AWS não deixa reduzir). Valores de produção para 2x o pico de
    5.000 TPS, a partir das escritas medidas por autorização (docs/capacidade.md): limites 11.000 escritas/s;
    autorizacoes 31.000 escritas/s e gsi_orfas 20.000; autorizacoes-outbox 61.000 (acima da quota padrão de 40.000
    WRU/s por tabela: pedir a quota antes, ou aplicar a alavanca 1 de docs/capacidade.md, que elimina o outbox).
  EOT
  type = map(object({
    leitura = optional(number)
    escrita = optional(number)
    indices = optional(map(object({
      leitura = optional(number)
      escrita = optional(number)
    })), {})
  }))
  default = {}

  validation {
    condition     = alltrue([for t in keys(var.warm_throughput) : contains(["limites", "autorizacoes", "autorizacoes-outbox", "contabilizacao-lancamentos", "contabilizacao-inbox"], t)])
    error_message = "warm_throughput: as chaves devem ser tabelas do módulo (limites, autorizacoes, autorizacoes-outbox, contabilizacao-lancamentos, contabilizacao-inbox)."
  }
}
