# Permissions boundary de toda role desta stack: criado pelo ambiente `esteira` (pré-requisito do deploy). A role de
# deploy da esteira só cria/altera role que carregue este boundary (sem escalada de privilégio via pipeline).
locals {
  limite_de_permissoes_arn = "arn:aws:iam::${var.account_id}:policy/desafio-zup-limite-de-permissoes"
  regiao                   = "sa-east-1"

  # Trust policy das 4 TASK roles com a condição anti-confused-deputy documentada pelo ECS (aws:SourceAccount e
  # aws:SourceArn com curinga: a AWS não suporta fixar o cluster nesta condição).
  # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html
  # As EXECUTION roles (módulo ecs_servico) ficam sem condição: a página da task execution role não a documenta.
  confianca_ecs_tasks = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${local.regiao}:${var.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })

  # Load shedding (MAX_EM_VOO): capacidade da task × p99 alvo × 2 (docs/capacidade.md). ~8 ms de CPU por autorização
  # (medido, 1.0.3) = ~125 rps por vCPU; 0,5 vCPU → 62,5 × 0,15 s × 2 ≈ 18. Deriva da CPU da task, para o portão
  # acompanhar o tamanho da task (antes era fixo em 32, calibrado para 1 vCPU). var.max_em_voo sobrescreve.
  max_em_voo = coalesce(var.max_em_voo, floor(var.cpu / 1024 * 125 * 0.15 * 2))
}

provider "aws" {
  region = "sa-east-1"
  # Local: profile desafio-zup. Na esteira (OIDC) as credenciais vêm do ambiente: TF_VAR_aws_profile="".
  profile             = var.aws_profile != "" ? var.aws_profile : null
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      environment = "sandbox"
      team        = var.time
      managed-by  = "terraform"
      project     = "desafio-autorizacao"
    }
  }
}

locals {
  contrato           = coalesce(var.contrato_openapi, "${path.module}/../../../../contracts/openapi/autorizacao-v1.yaml")
  zip_relay          = coalesce(var.zip_relay, "${path.module}/../../build/relay.zip")
  zip_contabilizacao = coalesce(var.zip_contabilizacao, "${path.module}/../../build/contabilizacao.zip")
}

# ---------------------------------------------------------------- módulos de base

# Rede (VPC, subnets públicas, endpoints gateway, um SG por serviço com a 8080 do limites só para os clientes dele).
module "rede" {
  source = "../../modules/rede"

  nome = "desafio"
}

# Tabelas DynamoDB (espelham o ambiente local).
module "dados" {
  source = "../../modules/dados"

  deletion_protection = var.deletion_protection
  warm_throughput     = var.warm_throughput
}

# Repositórios ECR.
module "ecr" {
  source = "../../modules/ecr"
}

# Bus, archive, filas, DLQs e schema registry dos contratos de evento.
module "eventos" {
  source = "../../modules/eventos"

  # A regra da data platform usa a mesma DLQ de entrega dos targets do bus.
  regras_externas_na_dlq_de_entrega = [module.plataforma_de_dados.regra_arn]
}

# Data platform (4º consumidor): regra do bus -> Firehose -> S3 particionado por tipo e data, com alarme de defasagem.
module "plataforma_de_dados" {
  source                   = "../../modules/plataforma_de_dados"
  limite_de_permissoes_arn = local.limite_de_permissoes_arn

  account_id         = var.account_id
  bus_nome           = module.eventos.bus_nome
  sources            = module.eventos.sources
  dlq_de_entrega_arn = module.eventos.entrega_dlq_arn
  ativa              = var.plataforma_de_dados_ativa
  forcar_destruicao  = !var.deletion_protection
  acoes_alarme       = [module.observabilidade.topico_alarmes_arn]
}

# ---------------------------------------------------------------- ECS

# Cluster ECS com Container Insights.
resource "aws_ecs_cluster" "this" {
  name = "desafio"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# Namespace Cloud Map (HTTP) do Service Connect: `limites` é publicado como limites:8080.
resource "aws_service_discovery_http_namespace" "this" {
  name        = "desafio.local"
  description = "Service Connect: chamadas east-west entre autorizacao e limites"
}

# Role da task `autorizacao`. Não existe ação IAM `dynamodb:TransactWriteItems`: a transação é
# autorizada pelas ações de cada item (PutItem/UpdateItem/ConditionCheckItem) em cada tabela envolvida.
resource "aws_iam_role" "task_autorizacao" {
  name                 = "desafio-task-autorizacao"
  permissions_boundary = local.limite_de_permissoes_arn
  assume_role_policy   = local.confianca_ecs_tasks
}

# Permissões da `autorizacao`: tabela de autorizações (itens) e PutItem no outbox (mesma transação).
resource "aws_iam_role_policy" "task_autorizacao" {
  name = "dynamodb"
  role = aws_iam_role.task_autorizacao.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Autorizacoes"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
        Resource = [module.dados.arns["autorizacoes"]]
      },
      {
        Sid      = "Outbox"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [module.dados.arns["autorizacoes-outbox"]]
      },
    ]
  })
}

# Role da task `limites`.
resource "aws_iam_role" "task_limites" {
  name                 = "desafio-task-limites"
  permissions_boundary = local.limite_de_permissoes_arn
  assume_role_policy   = local.confianca_ecs_tasks
}

# Permissões da `limites`: item único do agregado (UpdateItem condicional, ADR-0005).
resource "aws_iam_role_policy" "task_limites" {
  name = "dynamodb"
  role = aws_iam_role.task_limites.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "Limites"
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
      Resource = [module.dados.arns["limites"]]
    }]
  })
}

# Serviço `limites` (BC Limites): API interna publicada via Service Connect como limites:8080.
module "limites" {
  source                   = "../../modules/ecs_servico"
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  arquitetura_cpu          = var.arquitetura_cpu

  nome                     = "limites"
  cluster_arn              = aws_ecs_cluster.this.arn
  cluster_nome             = aws_ecs_cluster.this.name
  namespace_arn            = aws_service_discovery_http_namespace.this.arn
  imagem                   = "${module.ecr.urls["limites"]}:${var.tag_imagem_limites}"
  repositorio_ecr_arn      = module.ecr.arns["limites"]
  task_role_arn            = aws_iam_role.task_limites.arn
  subnet_ids               = module.rede.subnet_ids_publicas
  security_group_ids       = [module.rede.sg_ids["limites"]]
  cpu                      = var.cpu
  memoria                  = var.memoria
  desired_count            = var.tasks_desejadas
  min_tasks                = var.tasks_min
  max_tasks                = var.tasks_max
  service_connect_publicar = "limites"
  janelas_de_pico          = var.janelas_de_pico
  escala_preditiva         = var.modo_escala_preditiva

  variaveis_ambiente = {
    AWS_REGION     = "sa-east-1"
    TABELA_LIMITES = module.dados.nomes["limites"]
  }
}

# Serviço `autorizacao` (BC Autorização): atrás do NLB; cliente de `limites` via Service Connect.
module "autorizacao" {
  source                   = "../../modules/ecs_servico"
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  arquitetura_cpu          = var.arquitetura_cpu

  nome                = "autorizacao"
  cluster_arn         = aws_ecs_cluster.this.arn
  cluster_nome        = aws_ecs_cluster.this.name
  namespace_arn       = aws_service_discovery_http_namespace.this.arn
  imagem              = "${module.ecr.urls["autorizacao"]}:${var.tag_imagem_autorizacao}"
  repositorio_ecr_arn = module.ecr.arns["autorizacao"]
  task_role_arn       = aws_iam_role.task_autorizacao.arn
  subnet_ids          = module.rede.subnet_ids_publicas
  security_group_ids  = [module.rede.sg_ids["autorizacao"]]
  cpu                 = var.cpu
  memoria             = var.memoria
  desired_count       = var.tasks_desejadas
  min_tasks           = var.tasks_min
  max_tasks           = var.tasks_max
  target_group_arn    = module.api.target_group_arn
  janelas_de_pico     = var.janelas_de_pico
  escala_preditiva    = var.modo_escala_preditiva

  variaveis_ambiente = {
    AWS_REGION          = "sa-east-1"
    TABELA_AUTORIZACOES = module.dados.nomes["autorizacoes"]
    TABELA_OUTBOX       = module.dados.nomes["autorizacoes-outbox"]
    URL_LIMITES         = "http://limites:8080"
    # Teto de requisições em voo por task (load shedding → 503 imediato), derivado da CPU (local.max_em_voo).
    # Com 256 em voo a fila chegava a segundos sob sobrecarga (medido na AWS, 20/10).
    MAX_EM_VOO = tostring(local.max_em_voo)
    # Shards dos GSIs esparsos: o MESMO valor no escritor (aqui) e no leitor (reconciliador). Ver var.shards_dos_indices.
    SHARDS_DE_ORFAS       = tostring(var.shards_dos_indices)
    SHARDS_DE_VENCIMENTOS = tostring(var.shards_dos_indices)
  }

  # A chamada a limites só funciona depois que limites publica o alias no namespace.
  depends_on = [module.limites]
}

# Role da task `limites-acl` (ACL do legado, RN-05): consome o feed do legado e grava no limite.
resource "aws_iam_role" "task_limites_acl" {
  name                 = "desafio-task-limites-acl"
  permissions_boundary = local.limite_de_permissoes_arn
  assume_role_policy   = local.confianca_ecs_tasks
}

# Permissões da ACL: ler/apagar da fila do legado e Get/Put/Update na tabela limites. GetQueueUrl é
# usado pelo consumidor (resolve a URL pelo nome da fila).
resource "aws_iam_role_policy" "task_limites_acl" {
  name = "acl-legado"
  role = aws_iam_role.task_limites_acl.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "FilaDoLegado"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = [module.eventos.fila_legado_arn]
      },
      {
        Sid      = "Limites"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [module.dados.arns["limites"]]
      },
    ]
  })
}

# Serviço `limites-acl`: MESMA imagem do limites, command do consumidor da fila do legado. Sem NLB, sem
# HTTP, sem Service Connect; uma task (consumidores concorrentes seriam seguros: o CAS é por versão).
module "limites_acl" {
  source                   = "../../modules/ecs_servico"
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  arquitetura_cpu          = var.arquitetura_cpu

  nome                = "limites-acl"
  cluster_arn         = aws_ecs_cluster.this.arn
  cluster_nome        = aws_ecs_cluster.this.name
  namespace_arn       = aws_service_discovery_http_namespace.this.arn
  imagem              = "${module.ecr.urls["limites"]}:${var.tag_imagem_limites}"
  repositorio_ecr_arn = module.ecr.arns["limites"]
  task_role_arn       = aws_iam_role.task_limites_acl.arn
  subnet_ids          = module.rede.subnet_ids_publicas
  security_group_ids  = [module.rede.sg_ids["limites-acl"]]
  cpu                 = 256
  memoria             = 512
  desired_count       = 1
  min_tasks           = 1 # autoscaling fixo em 1 (o módulo sempre cria o alvo)
  max_tasks           = 1

  comando         = ["python", "-m", "limites.adapters.inbound.legado.consumidor"]
  expor_porta     = false
  service_connect = false
  # Worker grava o heartbeat em /tmp. No Fargate o volume efêmero montado em /tmp pertence ao root e o
  # container roda como uid 10001 (falhou no 1º deploy): usamos o /tmp da imagem (gravável por todos) e
  # deixamos o root fs gravável SÓ nos workers. As APIs seguem com root fs somente leitura.
  tmp_gravavel            = false
  root_fs_somente_leitura = false
  healthcheck_comando = [
    "python", "-c",
    "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/limites-acl.heartbeat')<30 else 1)",
  ]

  variaveis_ambiente = {
    AWS_REGION     = "sa-east-1"
    TABELA_LIMITES = module.dados.nomes["limites"]
    FILA_LEGADO    = module.eventos.fila_legado_nome
    # Sem AWS_ENDPOINT_URL o consumidor só sobe com este opt-in explícito (evita rodar na AWS por engano).
    ACL_MODO_AWS = "true"
  }
}

# Role da task `reconciliador` (ADR-0006).
resource "aws_iam_role" "task_reconciliador" {
  name                 = "desafio-task-reconciliador"
  permissions_boundary = local.limite_de_permissoes_arn
  assume_role_policy   = local.confianca_ecs_tasks
}

# Permissões do reconciliador: Query nos GSIs de órfãs e de vencimentos (expirador de reservas), Get/Put/Update em autorizacoes (assumir lease e concluir)
# e Put no outbox (a conclusão da compensação é uma transação que grava o evento).
resource "aws_iam_role_policy" "task_reconciliador" {
  name = "dynamodb"
  role = aws_iam_role.task_reconciliador.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IndicesDeOrfasEVencimentos"
        Effect = "Allow"
        Action = ["dynamodb:Query"]
        Resource = [
          "${module.dados.arns["autorizacoes"]}/index/gsi_orfas",
          "${module.dados.arns["autorizacoes"]}/index/gsi_vencimentos",
        ]
      },
      {
        Sid      = "Autorizacoes"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [module.dados.arns["autorizacoes"]]
      },
      {
        Sid      = "Outbox"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [module.dados.arns["autorizacoes-outbox"]]
      },
    ]
  })
}

# Serviço `reconciliador`: MESMA imagem da autorizacao, command do loop. É ECS (e não Lambda) porque chama o
# `limites` pelo Service Connect, que Lambda não alcança. Cliente do namespace; sem NLB.
module "reconciliador" {
  source                   = "../../modules/ecs_servico"
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  arquitetura_cpu          = var.arquitetura_cpu

  nome                = "reconciliador"
  cluster_arn         = aws_ecs_cluster.this.arn
  cluster_nome        = aws_ecs_cluster.this.name
  namespace_arn       = aws_service_discovery_http_namespace.this.arn
  imagem              = "${module.ecr.urls["autorizacao"]}:${var.tag_imagem_autorizacao}"
  repositorio_ecr_arn = module.ecr.arns["autorizacao"]
  task_role_arn       = aws_iam_role.task_reconciliador.arn
  subnet_ids          = module.rede.subnet_ids_publicas
  security_group_ids  = [module.rede.sg_ids["reconciliador"]]
  cpu                 = 256
  memoria             = 512
  desired_count       = 1
  min_tasks           = 1 # autoscaling fixo em 1 (o módulo sempre cria o alvo)
  max_tasks           = 1

  comando     = ["python", "-m", "autorizacao.adapters.inbound.reconciliador"]
  expor_porta = false
  # Mesmo motivo do limites-acl (heartbeat em /tmp com uid 10001).
  tmp_gravavel            = false
  root_fs_somente_leitura = false
  healthcheck_comando = [
    "python", "-c",
    "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/reconciliador.heartbeat')<90 else 1)",
  ]

  variaveis_ambiente = {
    AWS_REGION            = "sa-east-1"
    TABELA_AUTORIZACOES   = module.dados.nomes["autorizacoes"]
    TABELA_OUTBOX         = module.dados.nomes["autorizacoes-outbox"]
    URL_LIMITES           = "http://limites:8080"
    SHARDS_DE_ORFAS       = tostring(var.shards_dos_indices)
    SHARDS_DE_VENCIMENTOS = tostring(var.shards_dos_indices)
  }

  # A chamada a limites só funciona depois que limites publica o alias no namespace.
  depends_on = [module.limites]
}

# ---------------------------------------------------------------- API, assíncrono e observabilidade

# NLB + VPC Link + API Gateway REST + Cognito + usage plans + WAF.
module "api" {
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  source                   = "../../modules/api"

  account_id                = var.account_id
  vpc_id                    = module.rede.vpc_id
  subnet_ids                = module.rede.subnet_ids_publicas
  contrato_openapi          = local.contrato
  waf_limite_por_ip         = var.waf_limite_por_ip
  waf_limite_por_consumidor = var.waf_limite_por_consumidor
  plano_carga               = var.plano_carga
  deletion_protection       = var.deletion_protection
}

# ---------------------------------------------------------------- consumidores de referência (antifraude, notificações)

# Zip do pacote consumidores. Fica aqui, com o bloco que a usa; mesma semântica de var.zip_relay.

locals {
  zip_consumidores = coalesce(var.zip_consumidores, "${path.module}/../../build/consumidores.zip")

  # Toda fila de assinatura sem consumidor próprio (hoje antifraude e notificacoes; a contabilização tem o dela) ganha
  # o consumidor de referência: o evento não fica parado na fila, e o alarme idade-da-fila-<fila> passa a medir atraso
  # de consumo de verdade. As filas e as DLQs continuam do módulo eventos.
  consumidores_de_referencia = { for fila, arn in module.eventos.filas_arns : fila => arn if fila != "contabilizacao" }
}

# Funções Lambda: relay (outbox -> bus), relay-cdc-limites (stream de limites -> bus), sweeper (agendado), contabilização
# (SQS) e os consumidores de referência (SQS, uma função por fila).
module "lambdas" {
  limite_de_permissoes_arn = local.limite_de_permissoes_arn
  source                   = "../../modules/lambdas"
  account_id               = var.account_id
  regiao                   = local.regiao

  zip_relay               = local.zip_relay
  zip_contabilizacao      = local.zip_contabilizacao
  bus_nome                = module.eventos.bus_nome
  bus_arn                 = module.eventos.bus_arn
  outbox_tabela_nome      = module.dados.nomes["autorizacoes-outbox"]
  outbox_tabela_arn       = module.dados.arns["autorizacoes-outbox"]
  outbox_stream_arn       = module.dados.stream_arns["autorizacoes-outbox"]
  limites_stream_arn      = module.dados.stream_arns["limites"]
  fila_contabilizacao_arn = module.eventos.filas_arns["contabilizacao"]

  contabilizacao_tabelas = {
    lancamentos_nome = module.dados.nomes["contabilizacao-lancamentos"]
    lancamentos_arn  = module.dados.arns["contabilizacao-lancamentos"]
    inbox_nome       = module.dados.nomes["contabilizacao-inbox"]
    inbox_arn        = module.dados.arns["contabilizacao-inbox"]
  }

  consumidores        = local.consumidores_de_referencia
  zip_consumidores    = local.zip_consumidores
  deletion_protection = var.deletion_protection
}

# Dashboard e alarmes (burn rate do SLO, p99, DLQs, idade das filas, Errors e IteratorAge das Lambdas, sinais de log).
module "observabilidade" {
  source = "../../modules/observabilidade"

  account_id              = var.account_id
  regiao                  = local.regiao
  email_alarmes           = var.email_alarmes
  api_nome                = module.api.api_nome
  api_stage               = module.api.stage_nome
  tabelas                 = values(module.dados.nomes)
  tabelas_com_latencia    = [module.dados.nomes["limites"], module.dados.nomes["autorizacoes"]]
  cluster_nome            = aws_ecs_cluster.this.name
  servicos_ecs            = [module.autorizacao.nome, module.limites.nome]
  filas                   = concat(values(module.eventos.filas_nomes), [module.eventos.fila_legado_nome])
  fila_legado             = module.eventos.fila_legado_nome
  dlqs                    = concat(values(module.eventos.dlqs_nomes), [module.lambdas.relay_dlq_nome, module.lambdas.cdc_limites_dlq_nome, module.eventos.fila_legado_dlq_nome, module.eventos.entrega_dlq_nome])
  relay_funcao            = module.lambdas.relay_nome
  cdc_funcao              = module.lambdas.cdc_limites_nome
  log_group_limites       = module.limites.log_group_nome
  log_group_reconciliador = module.reconciliador.log_group_nome
  log_group_autorizacao   = module.autorizacao.log_group_nome
  log_group_cdc           = module.lambdas.cdc_limites_log_group_nome

  # Os consumidores de referência entram com a chave consumidor-<fila> (alarme lambda-erros-consumidor-<fila>).
  funcoes_lambda = merge(
    {
      relay          = module.lambdas.relay_nome
      sweeper        = module.lambdas.sweeper_nome
      cdc-limites    = module.lambdas.cdc_limites_nome
      contabilizacao = module.lambdas.contabilizacao_nome
    },
    module.lambdas.consumidores_funcoes,
  )
}
