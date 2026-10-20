# Bus de domínio onde o relay publica os CloudEvents do outbox (ADR-0007/0008).
resource "aws_cloudwatch_event_bus" "this" {
  name = var.nome_bus
}

# Archive do bus para replay. Sandbox: 30 dias. PRODUÇÃO: trilha de 10 anos via Firehose -> S3 com
# Object Lock (o archive do EventBridge não é a trilha regulatória).
resource "aws_cloudwatch_event_archive" "this" {
  name             = "${var.nome_bus}-archive"
  event_source_arn = aws_cloudwatch_event_bus.this.arn
  retention_days   = var.retencao_archive_dias
}

# DLQ de cada consumidor (retenção máxima de 14 dias, igual ao bootstrap local).
resource "aws_sqs_queue" "dlq" {
  for_each = var.assinaturas

  name                      = "${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# Fila de cada consumidor, com redrive para a DLQ após N recebimentos.
resource "aws_sqs_queue" "this" {
  for_each = var.assinaturas

  name                       = each.key
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = var.visibilidade_segundos
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Só a fila principal pode redirecionar para esta DLQ (evita uso indevido da DLQ por outras filas).
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  for_each = var.assinaturas

  queue_url = aws_sqs_queue.dlq[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this[each.key].arn]
  })
}

# Regra por consumidor: mesmo source e detail-types do bootstrap local.
resource "aws_cloudwatch_event_rule" "this" {
  for_each = var.assinaturas

  name           = "para-${each.key}"
  event_bus_name = aws_cloudwatch_event_bus.this.name
  state          = "ENABLED"

  event_pattern = jsonencode({
    source        = [var.source_eventos]
    "detail-type" = each.value
  })
}

# Target: a regra entrega na fila do consumidor.
resource "aws_cloudwatch_event_target" "this" {
  for_each = var.assinaturas

  rule           = aws_cloudwatch_event_rule.this[each.key].name
  event_bus_name = aws_cloudwatch_event_bus.this.name
  target_id      = each.key
  arn            = aws_sqs_queue.this[each.key].arn
}

# Queue policy: só o EventBridge, e só a partir da regra correspondente (aws:SourceArn), pode enviar.
resource "aws_sqs_queue_policy" "this" {
  for_each = var.assinaturas

  queue_url = aws_sqs_queue.this[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EventBridgeEnviaDaRegra"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.this[each.key].arn
      Condition = {
        # A regra da própria fila e, no consumidor de LimiteAlterado, também a regra do CDC de limites.
        ArnEquals = {
          "aws:SourceArn" = concat(
            [aws_cloudwatch_event_rule.this[each.key].arn],
            each.key == var.consumidor_limite_alterado ? [aws_cloudwatch_event_rule.limite_alterado.arn] : []
          )
        }
      }
    }]
  })
}

# Regra do LimiteAlterado (source financas.limites, publicado pelo relay-cdc-limites) -> fila do consumidor.
resource "aws_cloudwatch_event_rule" "limite_alterado" {
  name           = "limite-alterado-para-${var.consumidor_limite_alterado}"
  event_bus_name = aws_cloudwatch_event_bus.this.name
  state          = "ENABLED"

  event_pattern = jsonencode({
    source        = [var.source_limites]
    "detail-type" = ["LimiteAlterado"]
  })
}

# Target: entrega o LimiteAlterado na fila do consumidor.
resource "aws_cloudwatch_event_target" "limite_alterado" {
  rule           = aws_cloudwatch_event_rule.limite_alterado.name
  event_bus_name = aws_cloudwatch_event_bus.this.name
  target_id      = var.consumidor_limite_alterado
  arn            = aws_sqs_queue.this[var.consumidor_limite_alterado].arn
}

# ---------------------------------------------------------------- feed do core legado (RN-05)

# DLQ do feed do legado: registros inválidos (fora do layout) caem aqui após maxReceiveCount tentativas.
resource "aws_sqs_queue" "legado_dlq" {
  name                      = "${var.fila_legado}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# Fila do feed do legado (produtor: o core/legado-mock; consumidor: serviço ECS limites-acl).
resource "aws_sqs_queue" "legado" {
  name                       = var.fila_legado
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = var.visibilidade_segundos
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.legado_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Só a fila do legado pode redirecionar para a sua DLQ.
resource "aws_sqs_queue_redrive_allow_policy" "legado_dlq" {
  queue_url = aws_sqs_queue.legado_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.legado.arn]
  })
}
