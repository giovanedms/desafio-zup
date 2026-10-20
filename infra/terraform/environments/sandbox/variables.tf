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
  description = "Teto do WAF por IP em 5 minutos (429). Padrão 300.000 (1.000 rps): pega tráfego sem x-api-key e chaves falsas girando a partir de um IP, sem barrar um egress (NAT) com vários canais."
  type        = number
  default     = 300000
}

variable "waf_limite_por_consumidor" {
  description = "Teto do WAF por x-api-key em 5 minutos (429). Padrão 120.000 (400 rps = 2x o plano-padrao). A janela de medição com o canal-carga precisa subir este valor (e o waf_limite_por_ip, se o k6 sair de um IP só)."
  type        = number
  default     = 120000
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
  description = "Requisições em voo por task do autorizacao antes do load shedding (503). null (padrão) = derivado da CPU da task: capacidade (~125 rps por vCPU) × p99 alvo (0,15 s) × 2, ou seja 18 com 0,5 vCPU e 37 com 1 vCPU."
  type        = number
  default     = null
}

variable "shards_dos_indices" {
  description = "Shards dos GSIs esparsos gsi_orfas e gsi_vencimentos (SHARDS_DE_ORFAS/SHARDS_DE_VENCIMENTOS no autorizacao e no reconciliador). 64: ~156 WCU/s por chave a 5.000 TPS (teto 1.000). Pode SUBIR (leitor junto ou antes); nunca reduzir sem migrar."
  type        = number
  default     = 64

  validation {
    condition     = var.shards_dos_indices >= 10
    error_message = "shards_dos_indices não pode ficar abaixo de 10: os itens gravados com N = 10 sairiam do alcance do leitor."
  }
}

variable "janelas_de_pico" {
  description = "Janelas de pico previsível (scheduled scaling) aplicadas a autorizacao e limites. Exemplo do sandbox: dia 1 de cada mês, 06:30–12:00 (horário de Brasília), mínimo de 4 tasks em vez de 2."
  type = list(object({
    nome      = string
    inicio    = string
    fim       = string
    min_tasks = number
    max_tasks = optional(number)
  }))
  default = [{
    nome      = "inicio-de-mes"
    inicio    = "cron(30 6 1 * ? *)"
    fim       = "cron(0 12 1 * ? *)"
    min_tasks = 4
  }]
}

variable "warm_throughput" {
  description = "Warm throughput das tabelas (módulo dados). Vazio = desligado no sandbox: aumentar é cobrado e não pode ser desfeito. Valores de produção na descrição da variável do módulo e no ADR-0013."
  type = map(object({
    leitura = optional(number)
    escrita = optional(number)
    indices = optional(map(object({
      leitura = optional(number)
      escrita = optional(number)
    })), {})
  }))
  default = {}
}

variable "email_alarmes" {
  description = "E-mail assinado no tópico SNS de alarmes. Vazio (padrão) = sem assinatura."
  type        = string
  default     = ""
}
