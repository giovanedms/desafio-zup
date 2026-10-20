variable "repositorios" {
  description = "Nomes dos repositórios ECR."
  type        = set(string)
  default     = ["autorizacao", "limites", "relay"]
}

variable "prefixo" {
  description = "Prefixo dos repositórios (ex.: desafio-zup/)."
  type        = string
  default     = "desafio-zup/"
}

variable "imagens_mantidas" {
  description = "Quantidade de imagens mantidas pelo lifecycle."
  type        = number
  default     = 10
}

variable "force_delete" {
  description = "Permite destruir o repositório mesmo com imagens (sandbox)."
  type        = bool
  default     = true
}
