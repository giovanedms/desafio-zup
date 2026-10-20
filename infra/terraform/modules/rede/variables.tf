variable "nome" {
  description = "Prefixo dos recursos de rede."
  type        = string
}

variable "cidr" {
  description = "CIDR da VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "zonas" {
  description = "Duas AZs onde ficam as subnets públicas (explícitas para o plano ser determinístico)."
  type        = list(string)
  default     = ["sa-east-1a", "sa-east-1c"]

  validation {
    condition     = length(var.zonas) == 2
    error_message = "Informe exatamente 2 AZs."
  }
}

variable "regiao" {
  description = "Região AWS (usada nos nomes dos gateway endpoints)."
  type        = string
  default     = "sa-east-1"
}
