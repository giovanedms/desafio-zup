variable "account_id" {
  description = "ID da conta AWS permitida (trava contra apply na conta errada). Obrigatório, sem default."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id deve ter 12 dígitos."
  }
}

variable "aws_profile" {
  description = "Profile do AWS CLI usado pelo provider."
  type        = string
  default     = "desafio-zup"
}

variable "time" {
  description = "Valor da tag obrigatória `team`."
  type        = string
  default     = "plataforma-financas"
}

variable "tag_imagem_autorizacao" {
  description = "Tag IMUTÁVEL da imagem do serviço autorizacao no ECR."
  type        = string
  default     = "1.0.0"
}

variable "tag_imagem_limites" {
  description = "Tag IMUTÁVEL da imagem do serviço limites no ECR."
  type        = string
  default     = "1.0.0"
}

variable "tasks_desejadas" {
  description = "Tasks iniciais de cada serviço ECS (o autoscaling assume depois)."
  type        = number
  default     = 2
}

variable "tasks_min" {
  description = "Mínimo do autoscaling."
  type        = number
  default     = 2
}

variable "tasks_max" {
  description = "Máximo do autoscaling."
  type        = number
  default     = 20
}

variable "cpu" {
  description = "CPU por task (unidades Fargate)."
  type        = number
  default     = 512
}

variable "memoria" {
  description = "Memória por task (MiB)."
  type        = number
  default     = 1024
}

variable "waf_limite_por_ip" {
  description = "Limite do WAF por IP em 5 minutos."
  type        = number
  default     = 2000
}

variable "deletion_protection" {
  description = "Proteção contra exclusão (tabelas e NLB). false no sandbox para o destroy funcionar."
  type        = bool
  default     = false
}

variable "contrato_openapi" {
  description = "Caminho do contrato OpenAPI. null = contracts/openapi/autorizacao-v1.yaml do repositório."
  type        = string
  default     = null
}

variable "zip_relay" {
  description = "Zip do pacote relay. null = infra/terraform/build/relay.zip (make pacotes)."
  type        = string
  default     = null
}

variable "zip_contabilizacao" {
  description = "Zip do pacote contabilizacao. null = infra/terraform/build/contabilizacao.zip (make pacotes)."
  type        = string
  default     = null
}

variable "arquitetura_cpu" {
  description = "Arquitetura das tasks ECS. ARM64 em produção; o sandbox pode usar X86_64 se as imagens forem construídas em máquina x86 sem emulação."
  type        = string
  default     = "ARM64"
}

variable "max_em_voo" {
  description = "Requisições em voo por task do autorizacao antes do load shedding (503). Dimensionar como capacidade da task × p99 alvo × 2."
  type        = number
  default     = 32
}
