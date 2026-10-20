variable "repositorios" {
  description = "Nomes dos repositórios ECR. Só as imagens que rodam na AWS: o relay é Lambda zip, e a imagem dele só existe no ambiente local."
  type        = set(string)
  default     = ["autorizacao", "limites"]
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
