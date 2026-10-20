variable "account_id" {
  description = "ID da conta AWS permitida (trava contra apply na conta errada). Obrigatório, sem default."
  type        = string
}

variable "aws_profile" {
  description = "Profile do AWS CLI do administrador que aplica este ambiente."
  type        = string
  default     = "desafio-zup"
}

variable "repositorio_github" {
  description = "Repositório (dono/nome) cujos workflows podem assumir a role de deploy."
  type        = string
  default     = "giovanedms/desafio-zup"
}

variable "branch_de_deploy" {
  description = "Única branch cujos workflows podem assumir a role (claim `sub` do token OIDC)."
  type        = string
  default     = "main"
}

variable "time" {
  description = "Valor da tag obrigatória `team`."
  type        = string
  default     = "plataforma-financas"
}
