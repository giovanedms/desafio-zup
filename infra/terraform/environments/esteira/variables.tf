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

variable "id_dono_github" {
  description = "ID numérico (imutável) do dono do repositório no GitHub: `gh api repos/<dono>/<repo> --jq .owner.id`."
  type        = string
  default     = "194153057"
}

variable "id_repositorio_github" {
  description = "ID numérico (imutável) do repositório no GitHub: `gh api repos/<dono>/<repo> --jq .id`."
  type        = string
  default     = "1428559478"
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
