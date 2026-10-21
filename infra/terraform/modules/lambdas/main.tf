locals {
  nomes = {
    relay          = "${var.prefixo}-relay"
    sweeper        = "${var.prefixo}-sweeper"
    cdc_limites    = "${var.prefixo}-relay-cdc-limites"
    contabilizacao = "${var.prefixo}-contabilizacao"
  }

  assume_lambda = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  # Anti-confused-deputy documentado pelo EventBridge Scheduler: aws:SourceAccount + aws:SourceArn do schedule GROUP
  # (não do schedule). O sweeper usa o grupo `default`.
  # https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html
  # A role do Lambda (assume_lambda acima) fica SEM condição: a documentação da execution role do Lambda não traz
  # aws:SourceAccount/SourceArn, e uma condição não suportada impediria a função de assumir a role.
  assume_scheduler = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.account_id
          "aws:SourceArn"     = "arn:aws:scheduler:${var.regiao}:${var.account_id}:schedule-group/default"
        }
      }
    }]
  })

  # Variáveis de ambiente esperadas pelo código: relay (BUS, TABELA_OUTBOX).
  # contabilizacao: TABELA_LANCAMENTOS e TABELA_INBOX (contabilizacao/config.py).
  env_relay = {
    BUS           = var.bus_nome
    TABELA_OUTBOX = var.outbox_tabela_nome
  }
  # relay-cdc-limites: só publica no bus (não há outbox nesta fonte).
  env_cdc_limites = {
    BUS = var.bus_nome
  }
  env_contabilizacao = {
    TABELA_LANCAMENTOS = var.contabilizacao_tabelas.lancamentos_nome
    TABELA_INBOX       = var.contabilizacao_tabelas.inbox_nome
  }
}

# Log groups das funções (criados aqui para a role escrever só neles e a retenção ser controlada).
resource "aws_cloudwatch_log_group" "this" {
  #checkov:skip=CKV_AWS_338:Retenção de 7 dias é desvio consciente do sandbox (custo); produção >= 365 dias.
  #checkov:skip=CKV_AWS_158:Sandbox usa a criptografia padrão do CloudWatch Logs; produção usa CMK.
  for_each = local.nomes

  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.retencao_logs_dias
}

# DLQ do relay: destino on-failure do ESM do stream (lotes que esgotam as tentativas).
resource "aws_sqs_queue" "relay_dlq" {
  name                      = "relay-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# ---------------------------------------------------------------- relay + sweeper (mesmo pacote e mesmas permissões)

# Role do relay/sweeper.
resource "aws_iam_role" "relay" {
  name                 = "${local.nomes.relay}-role"
  assume_role_policy   = local.assume_lambda
  permissions_boundary = var.limite_de_permissoes_arn
}

# Permissões mínimas: ler o stream do outbox, apagar/varrer o outbox, publicar no bus, DLQ e logs.
resource "aws_iam_role_policy" "relay" {
  name = "relay"
  role = aws_iam_role.relay.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "LerStreamDoOutbox"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"]
        Resource = [var.outbox_stream_arn]
      },
      {
        # ListStreams não aceita restrição de recurso (exigida pelo Lambda ESM).
        Sid      = "ListarStreams"
        Effect   = "Allow"
        Action   = ["dynamodb:ListStreams"]
        Resource = "*"
      },
      {
        # O código apaga via batch_write_item (DeleteRequest) e varre com Scan (sweeper).
        Sid      = "OutboxApagarEVarrer"
        Effect   = "Allow"
        Action   = ["dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:Scan"]
        Resource = [var.outbox_tabela_arn]
      },
      {
        Sid      = "PublicarNoBus"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = [var.bus_arn]
      },
      {
        Sid      = "DlqOnFailure"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.relay_dlq.arn]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.this["relay"].arn}:*", "${aws_cloudwatch_log_group.this["sweeper"].arn}:*"]
      },
      {
        # X-Ray não aceita restrição de recurso.
        Sid      = "XRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

# Função relay: consome o stream do outbox e publica no bus.
resource "aws_lambda_function" "relay" {
  #checkov:skip=CKV_AWS_117:Funções fora da VPC de propósito: sandbox sem NAT, e só falam com APIs AWS públicas.
  #checkov:skip=CKV_AWS_116:Falhas do relay vão para a DLQ do ESM (on-failure destination); não há invocação assíncrona.
  #checkov:skip=CKV_AWS_115:Sem concorrência reservada: a conta tem 1.000 (em vigor, ADR-0004) compartilhadas e a reserva tiraria do pool das outras funções; o ESM limita a vazão.
  #checkov:skip=CKV_AWS_173:Variáveis de ambiente não são sensíveis; criptografia padrão basta no sandbox.
  #checkov:skip=CKV_AWS_272:Assinatura de código fora do escopo do sandbox.
  function_name    = local.nomes.relay
  role             = aws_iam_role.relay.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "relay.lambda_handler.handler"
  filename         = var.zip_relay
  source_code_hash = filebase64sha256(var.zip_relay)
  memory_size      = var.memoria_mb
  timeout          = var.timeout_segundos

  environment {
    variables = local.env_relay
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["relay"].name
  }

  depends_on = [aws_iam_role_policy.relay]
}

# Função sweeper: republica o que ficou no outbox (relay parado ou stream expirado).
resource "aws_lambda_function" "sweeper" {
  #checkov:skip=CKV_AWS_117:Funções fora da VPC de propósito: sandbox sem NAT, e só falam com APIs AWS públicas.
  #checkov:skip=CKV_AWS_116:Execução agendada; a próxima rodada (5 min) reprocessa, e o relay é idempotente.
  #checkov:skip=CKV_AWS_115:Sem concorrência reservada: a conta tem 1.000 (em vigor, ADR-0004) compartilhadas e a reserva tiraria do pool das outras funções; uma execução agendada a cada 5 min.
  #checkov:skip=CKV_AWS_173:Variáveis de ambiente não são sensíveis; criptografia padrão basta no sandbox.
  #checkov:skip=CKV_AWS_272:Assinatura de código fora do escopo do sandbox.
  function_name    = local.nomes.sweeper
  role             = aws_iam_role.relay.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "relay.sweeper.handler_agendado"
  filename         = var.zip_relay
  source_code_hash = filebase64sha256(var.zip_relay)
  memory_size      = var.memoria_mb
  timeout          = 60

  environment {
    variables = local.env_relay
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["sweeper"].name
  }

  depends_on = [aws_iam_role_policy.relay]
}

# Liga o stream do outbox ao relay: lote de 100, falha parcial, bisect e destino on-failure. O filtro entrega só INSERT
# (o único que o relay publica, outbox_dynamodb.py): o REMOVE que o próprio relay gera ao apagar o item não invoca a função.
resource "aws_lambda_event_source_mapping" "relay" {
  event_source_arn                   = var.outbox_stream_arn
  function_name                      = aws_lambda_function.relay.arn
  starting_position                  = "LATEST"
  batch_size                         = 100
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
  bisect_batch_on_function_error     = true
  maximum_retry_attempts             = 5

  filter_criteria {
    filter {
      pattern = jsonencode({ eventName = ["INSERT"] })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.relay_dlq.arn
    }
  }

  depends_on = [aws_iam_role_policy.relay]
}

# Role do EventBridge Scheduler: só pode invocar o sweeper.
resource "aws_iam_role" "scheduler" {
  name                 = "${local.nomes.sweeper}-scheduler"
  assume_role_policy   = local.assume_scheduler
  permissions_boundary = var.limite_de_permissoes_arn
}

# Permissão mínima do scheduler: lambda:InvokeFunction no ARN do sweeper.
resource "aws_iam_role_policy" "scheduler" {
  name = "invocar-sweeper"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.sweeper.arn]
    }]
  })
}

# Agenda do sweeper: a cada 5 minutos.
resource "aws_scheduler_schedule" "sweeper" {
  #checkov:skip=CKV_AWS_297:Sandbox usa a criptografia padrão do Scheduler; a agenda não carrega dado sensível. Produção usa CMK.
  name                = "${local.nomes.sweeper}-5min"
  group_name          = "default" # a trust policy da role do scheduler aceita só este grupo (aws:SourceArn)
  schedule_expression = "rate(5 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.sweeper.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# ---------------------------------------------------------------- relay-cdc-limites (Stream da tabela limites -> LimiteAlterado)

# DLQ do CDC de limites: destino on-failure do ESM (lotes que esgotam as tentativas).
resource "aws_sqs_queue" "cdc_limites_dlq" {
  name                      = "relay-cdc-limites-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# Role do relay-cdc-limites (separada da role do relay: não toca o outbox).
resource "aws_iam_role" "cdc_limites" {
  name                 = "${local.nomes.cdc_limites}-role"
  assume_role_policy   = local.assume_lambda
  permissions_boundary = var.limite_de_permissoes_arn
}

# Permissões mínimas: ler o stream da tabela limites, publicar no bus, DLQ e logs.
resource "aws_iam_role_policy" "cdc_limites" {
  name = "cdc-limites"
  role = aws_iam_role.cdc_limites.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "LerStreamDeLimites"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"]
        Resource = [var.limites_stream_arn]
      },
      {
        # ListStreams não aceita restrição de recurso (exigida pelo Lambda ESM).
        Sid      = "ListarStreams"
        Effect   = "Allow"
        Action   = ["dynamodb:ListStreams"]
        Resource = "*"
      },
      {
        Sid      = "PublicarNoBus"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = [var.bus_arn]
      },
      {
        Sid      = "DlqOnFailure"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.cdc_limites_dlq.arn]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.this["cdc_limites"].arn}:*"]
      },
      {
        # X-Ray não aceita restrição de recurso.
        Sid      = "XRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

# Função relay-cdc-limites: mesmo pacote do relay, handler próprio (MODIFY de concedido/status -> LimiteAlterado).
resource "aws_lambda_function" "cdc_limites" {
  #checkov:skip=CKV_AWS_117:Funções fora da VPC de propósito: sandbox sem NAT, e só falam com APIs AWS públicas.
  #checkov:skip=CKV_AWS_116:Falhas vão para a DLQ do ESM (on-failure destination); não há invocação assíncrona.
  #checkov:skip=CKV_AWS_115:Sem concorrência reservada: a conta tem 1.000 (em vigor, ADR-0004) compartilhadas e a reserva tiraria do pool das outras funções; o ESM limita a vazão.
  #checkov:skip=CKV_AWS_173:Variáveis de ambiente não são sensíveis; criptografia padrão basta no sandbox.
  #checkov:skip=CKV_AWS_272:Assinatura de código fora do escopo do sandbox.
  function_name    = local.nomes.cdc_limites
  role             = aws_iam_role.cdc_limites.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "relay.lambda_handler.handler_cdc_limites"
  filename         = var.zip_relay
  source_code_hash = filebase64sha256(var.zip_relay)
  memory_size      = var.memoria_mb
  timeout          = var.timeout_segundos

  environment {
    variables = local.env_cdc_limites
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["cdc_limites"].name
  }

  depends_on = [aws_iam_role_policy.cdc_limites]
}

# Liga o stream da tabela limites à função; o filtro entrega ao Lambda só MODIFY (INSERT/REMOVE nem invocam).
resource "aws_lambda_event_source_mapping" "cdc_limites" {
  event_source_arn                   = var.limites_stream_arn
  function_name                      = aws_lambda_function.cdc_limites.arn
  starting_position                  = "LATEST"
  batch_size                         = 100
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
  bisect_batch_on_function_error     = true
  maximum_retry_attempts             = 5

  filter_criteria {
    filter {
      pattern = jsonencode({ eventName = ["MODIFY"] })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.cdc_limites_dlq.arn
    }
  }

  depends_on = [aws_iam_role_policy.cdc_limites]
}

# ---------------------------------------------------------------- contabilização

# Role da contabilização.
resource "aws_iam_role" "contabilizacao" {
  name                 = "${local.nomes.contabilizacao}-role"
  assume_role_policy   = local.assume_lambda
  permissions_boundary = var.limite_de_permissoes_arn
}

# Permissões mínimas: consumir a fila, gravar/ler as duas tabelas da contabilização, logs e X-Ray.
resource "aws_iam_role_policy" "contabilizacao" {
  name = "contabilizacao"
  role = aws_iam_role.contabilizacao.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumirFila"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = [var.fila_contabilizacao_arn]
      },
      {
        Sid    = "TabelasDaContabilizacao"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:ConditionCheckItem",
          "dynamodb:UpdateItem",
        ]
        Resource = [var.contabilizacao_tabelas.lancamentos_arn, var.contabilizacao_tabelas.inbox_arn]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.this["contabilizacao"].arn}:*"]
      },
      {
        # X-Ray não aceita restrição de recurso.
        Sid      = "XRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

# Função de contabilização (consumidor idempotente da fila).
resource "aws_lambda_function" "contabilizacao" {
  #checkov:skip=CKV_AWS_117:Funções fora da VPC de propósito: sandbox sem NAT, e só falam com APIs AWS públicas.
  #checkov:skip=CKV_AWS_116:Falhas voltam à fila e vão para a DLQ pelo redrive (maxReceiveCount 5).
  #checkov:skip=CKV_AWS_115:Sem concorrência reservada: a conta tem 1.000 (em vigor, ADR-0004) compartilhadas e a reserva tiraria do pool das outras funções; o ESM limita a concorrência (maximum_concurrency).
  #checkov:skip=CKV_AWS_173:Variáveis de ambiente não são sensíveis; criptografia padrão basta no sandbox.
  #checkov:skip=CKV_AWS_272:Assinatura de código fora do escopo do sandbox.
  function_name    = local.nomes.contabilizacao
  role             = aws_iam_role.contabilizacao.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "contabilizacao.adapters.inbound.lambda_sqs.handler"
  filename         = var.zip_contabilizacao
  source_code_hash = filebase64sha256(var.zip_contabilizacao)
  memory_size      = var.memoria_mb
  timeout          = var.timeout_segundos

  environment {
    variables = local.env_contabilizacao
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this["contabilizacao"].name
  }

  depends_on = [aws_iam_role_policy.contabilizacao]
}

# Liga a fila contabilizacao à função, com resposta parcial de lote.
resource "aws_lambda_event_source_mapping" "contabilizacao" {
  event_source_arn        = var.fila_contabilizacao_arn
  function_name           = aws_lambda_function.contabilizacao.arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.concorrencia_maxima_sqs
  }

  depends_on = [aws_iam_role_policy.contabilizacao]
}
