variable "nome" {
  description = "Nome do serviço (também usado no Service Connect quando publicado)."
  type        = string
}

variable "cluster_arn" {
  description = "ARN do cluster ECS."
  type        = string
}

variable "cluster_nome" {
  description = "Nome do cluster ECS (para o resource_id do autoscaling)."
  type        = string
}

variable "namespace_arn" {
  description = "ARN do namespace Cloud Map (HTTP) usado pelo Service Connect."
  type        = string
}

variable "imagem" {
  description = "URI completa da imagem (repositório:tag imutável)."
  type        = string
}

variable "repositorio_ecr_arn" {
  description = "ARN do repositório ECR (a execution role só pode puxar dele)."
  type        = string
}

variable "task_role_arn" {
  description = "ARN da task role (permissões da aplicação, definida pelo chamador)."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets onde as tasks rodam."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups das tasks."
  type        = list(string)
}

variable "regiao" {
  description = "Região AWS (driver awslogs)."
  type        = string
  default     = "sa-east-1"
}

variable "porta" {
  description = "Porta do container."
  type        = number
  default     = 8080
}

variable "cpu" {
  description = "CPU da task (unidades Fargate)."
  type        = number
  default     = 512
}

variable "memoria" {
  description = "Memória da task (MiB)."
  type        = number
  default     = 1024
}

variable "variaveis_ambiente" {
  description = "Variáveis de ambiente do container."
  type        = map(string)
  default     = {}
}

variable "desired_count" {
  description = "Número inicial de tasks (depois o autoscaling assume)."
  type        = number
  default     = 2
}

variable "min_tasks" {
  description = "Mínimo do autoscaling."
  type        = number
  default     = 2
}

variable "max_tasks" {
  description = "Máximo do autoscaling."
  type        = number
  default     = 20
}

variable "alvo_cpu_percent" {
  description = "Alvo de CPU média do target tracking."
  type        = number
  default     = 60
}

variable "service_connect_publicar" {
  description = "Nome DNS publicado no Service Connect (ex.: limites => limites:8080). null = apenas cliente."
  type        = string
  default     = null
}

variable "target_group_arn" {
  description = "Target group do NLB (opcional). null = sem load balancer."
  type        = string
  default     = null
}

variable "retencao_logs_dias" {
  description = "Retenção do log group."
  type        = number
  default     = 7
}

variable "root_fs_somente_leitura" {
  description = "Container com filesystem raiz somente leitura (hardening). Desligue se a aplicação precisar gravar em disco."
  type        = bool
  default     = true
}

variable "force_new_deployment" {
  description = "Força novo deployment a cada apply (útil quando a tag de imagem é reaproveitada)."
  type        = bool
  default     = false
}

variable "comando" {
  description = "Command do container (sobrescreve o CMD da imagem). null = usa o CMD da imagem. Usado pelos workers (ACL do legado, reconciliador)."
  type        = list(string)
  default     = null
}

variable "expor_porta" {
  description = "Publica a porta HTTP do container. false para workers sem HTTP (loop com heartbeat)."
  type        = bool
  default     = true
}

variable "healthcheck_comando" {
  description = "Comando do healthcheck do container (CMD sem o prefixo). null = GET /saude na porta HTTP."
  type        = list(string)
  default     = null
}

variable "tmp_gravavel" {
  description = "Monta um volume efêmero em /tmp (o root fs segue somente leitura). Necessário para o heartbeat dos workers."
  type        = bool
  default     = false
}

variable "service_connect" {
  description = "Habilita Service Connect (cliente do namespace). false para workers que só falam com DynamoDB/SQS."
  type        = bool
  default     = true
}

variable "arquitetura_cpu" {
  description = "ARM64 (Graviton, padrão de produção — ADR-0004) ou X86_64 (quando a imagem é construída numa máquina x86 sem emulação arm64)."
  type        = string
  default     = "ARM64"
  validation {
    condition     = contains(["ARM64", "X86_64"], var.arquitetura_cpu)
    error_message = "arquitetura_cpu deve ser ARM64 ou X86_64."
  }
}

variable "limite_de_permissoes_arn" {
  description = "ARN do permissions boundary aplicado a toda role criada aqui (teto de permissões; vem do ambiente `esteira`)."
  type        = string
}

variable "janelas_de_pico" {
  description = "Picos previsíveis (início de mês, datas de pagamento): em `inicio` o mínimo de tasks sobe para `min_tasks` (e o máximo para `max_tasks`, se informado); em `fim` volta ao mínimo/máximo de base. `inicio`/`fim` no formato do Application Auto Scaling, ex.: cron(30 6 1 * ? *). Vazio = só target tracking."
  type = list(object({
    nome      = string
    inicio    = string
    fim       = string
    min_tasks = number
    max_tasks = optional(number)
  }))
  default = []

  validation {
    condition = alltrue([
      for j in var.janelas_de_pico :
      j.min_tasks >= 1 && j.min_tasks <= coalesce(j.max_tasks, var.max_tasks) && can(regex("^(cron|at|rate)\\(.+\\)$", j.inicio)) && can(regex("^(cron|at|rate)\\(.+\\)$", j.fim))
    ])
    error_message = "Cada janela precisa de 1 <= min_tasks <= máximo e de inicio/fim no formato cron(...), at(...) ou rate(...)."
  }

  validation {
    condition     = length(var.janelas_de_pico) == length(distinct([for j in var.janelas_de_pico : j.nome]))
    error_message = "Os nomes das janelas de pico devem ser únicos."
  }
}

variable "fuso_horario" {
  description = "Fuso (IANA) em que as expressões das janelas de pico são avaliadas."
  type        = string
  default     = "America/Sao_Paulo"
}

variable "escala_preditiva" {
  description = "Modo da política de predictive scaling na CPU (alvo = alvo_cpu_percent): ForecastOnly (só previsão, sem agir) ou ForecastAndScale. null = sem política preditiva (workers de 1 task)."
  type        = string
  default     = null

  validation {
    condition     = contains(["ForecastOnly", "ForecastAndScale"], coalesce(var.escala_preditiva, "ForecastOnly"))
    error_message = "escala_preditiva deve ser null, ForecastOnly ou ForecastAndScale."
  }
}
