locals {
  assume_ecs_tasks = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Log group do serviço (retenção curta no sandbox).
resource "aws_cloudwatch_log_group" "this" {
  #checkov:skip=CKV_AWS_338:Retenção de 7 dias é desvio consciente do sandbox (custo); produção >= 365 dias.
  #checkov:skip=CKV_AWS_158:Sandbox usa a criptografia padrão do CloudWatch Logs; produção usa CMK.
  name              = "/ecs/${var.nome}"
  retention_in_days = var.retencao_logs_dias
}

# Execution role: usada pelo agente ECS para puxar a imagem e escrever logs (NÃO é a role da aplicação).
resource "aws_iam_role" "execucao" {
  name                 = "${var.nome}-ecs-execucao"
  assume_role_policy   = local.assume_ecs_tasks
  permissions_boundary = var.limite_de_permissoes_arn
}

# Permissões mínimas da execution role: pull do repositório específico e logs do log group específico.
resource "aws_iam_role_policy" "execucao" {
  name = "pull-ecr-e-logs"
  role = aws_iam_role.execucao.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken não aceita restrição de recurso.
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = [var.repositorio_ecr_arn]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.this.arn}:*"]
      },
    ]
  })
}

# Task definition Fargate (ARM64/Graviton por padrão), um container na porta da aplicação.
resource "aws_ecs_task_definition" "this" {
  family                   = var.nome
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memoria
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.arquitetura_cpu
  }

  # Volume efêmero em /tmp para o heartbeat dos workers (o root fs continua somente leitura).
  dynamic "volume" {
    for_each = var.tmp_gravavel ? ["tmp"] : []

    content {
      name = volume.value
    }
  }

  container_definitions = jsonencode([merge(
    {
      name                   = var.nome
      image                  = var.imagem
      essential              = true
      readonlyRootFilesystem = var.root_fs_somente_leitura
      user                   = "10001:10001"
      portMappings = var.expor_porta ? [{
        name          = "http"
        containerPort = var.porta
        protocol      = "tcp"
        appProtocol   = "http"
      }] : []
      mountPoints = var.tmp_gravavel ? [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }] : []
      environment = [for k, v in var.variaveis_ambiente : { name = k, value = v }]
      # O HEALTHCHECK do Dockerfile é ignorado pelo ECS: repetimos aqui (stdlib, a imagem slim não tem curl).
      healthCheck = {
        command     = var.healthcheck_comando != null ? concat(["CMD"], var.healthcheck_comando) : ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.porta}/saude', timeout=2)"]
        interval    = 15
        timeout     = 3
        retries     = 3
        startPeriod = var.healthcheck_comando != null ? 30 : 10
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.regiao
          "awslogs-stream-prefix" = var.nome
        }
      }
    },
    var.comando == null ? {} : { command = var.comando },
  )])
}

# Service Fargate: IP público (sandbox sem NAT, ver módulo rede), Service Connect e circuit breaker.
resource "aws_ecs_service" "this" {
  #checkov:skip=CKV_AWS_333:Desvio consciente do sandbox: IP público porque não há NAT (ADR-0004); a entrada segue fechada pelo SG.
  name                  = var.nome
  cluster               = var.cluster_arn
  task_definition       = aws_ecs_task_definition.this.arn
  desired_count         = var.desired_count
  launch_type           = "FARGATE"
  platform_version      = "LATEST"
  force_new_deployment  = var.force_new_deployment
  wait_for_steady_state = false

  # Mantém a capacidade durante o rollout e permite reversão automática de deployment ruim.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = true # DESVIO DO SANDBOX: sem NAT (ADR-0004); produção usa subnets privadas.
  }

  dynamic "service_connect_configuration" {
    for_each = var.service_connect ? [1] : []

    content {
      enabled   = true
      namespace = var.namespace_arn

      # Só quem publica (limites) declara o serviço; os demais são apenas clientes do namespace.
      dynamic "service" {
        for_each = var.service_connect_publicar == null ? [] : [var.service_connect_publicar]

        content {
          port_name      = "http"
          discovery_name = service.value

          client_alias {
            port     = var.porta
            dns_name = service.value
          }
        }
      }

      log_configuration {
        log_driver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.regiao
          "awslogs-stream-prefix" = "${var.nome}-service-connect"
        }
      }
    }
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]

    content {
      target_group_arn = load_balancer.value
      container_name   = var.nome
      container_port   = var.porta
    }
  }

  # O autoscaling controla a contagem depois da criação; sem isso todo apply reverteria o scale-out.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

# Alvo de autoscaling do service (2 a 20 tasks).
resource "aws_appautoscaling_target" "this" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${var.cluster_nome}/${aws_ecs_service.this.name}"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

# Target tracking de CPU a 60%. O scale-out reativo leva ~2 min para servir e, na medição, pediu tasks ~6 min depois
# do início da sobrecarga (ADR-0004, docs/capacidade.md): para o pico previsível há as janelas agendadas abaixo.
# Predictive scaling (aws_appautoscaling_policy PredictiveScaling, disponível em sa-east-1) fica como evolução:
# precisa de histórico de carga, que o sandbox não tem.
resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.nome}-cpu-alvo"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  resource_id        = aws_appautoscaling_target.this.resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = var.alvo_cpu_percent
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

locals {
  janelas_de_pico = { for j in var.janelas_de_pico : j.nome => j }
}

# Pico previsível (RN-04): no início de cada janela o MÍNIMO sobe (capacidade quente antes do tráfego chegar, sem
# esperar o target tracking); o target tracking continua agindo entre o mínimo e o máximo.
# Depende da política de CPU: chamadas simultâneas no mesmo alvo de autoscaling podem ser recusadas pela API
# (ConcurrentUpdateException) e o provider só repete ObjectNotFoundException.
resource "aws_appautoscaling_scheduled_action" "inicio_do_pico" {
  for_each = local.janelas_de_pico

  name               = "${var.nome}-${each.key}-inicio"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  resource_id        = aws_appautoscaling_target.this.resource_id
  schedule           = each.value.inicio
  timezone           = var.fuso_horario

  scalable_target_action {
    min_capacity = each.value.min_tasks
    max_capacity = coalesce(each.value.max_tasks, var.max_tasks)
  }

  depends_on = [aws_appautoscaling_policy.cpu]
}

# Fim da janela: volta ao mínimo e ao máximo de base. Criado depois do início (mesmo motivo acima). Atenção: um apply
# DENTRO da janela também devolve o alvo ao mínimo de base (min_capacity do aws_appautoscaling_target); ver runbook R3.
resource "aws_appautoscaling_scheduled_action" "fim_do_pico" {
  for_each = local.janelas_de_pico

  name               = "${var.nome}-${each.key}-fim"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  resource_id        = aws_appautoscaling_target.this.resource_id
  schedule           = each.value.fim
  timezone           = var.fuso_horario

  scalable_target_action {
    min_capacity = var.min_tasks
    max_capacity = var.max_tasks
  }

  depends_on = [aws_appautoscaling_scheduled_action.inicio_do_pico]
}
