# Data platform (4º consumidor do enunciado, ADR-0009): regra do bus -> Amazon Data Firehose (Direct PUT) -> S3.
# Sandbox: bucket com expiração de 30 dias. PRODUÇÃO: trilha de 10 anos neste mesmo caminho, com Object Lock em modo
# COMPLIANCE no bucket e sem expiração antes da retenção (ADR-0017); o archive do bus continua sendo só o replay.
# Carga: a regra casa todo evento (+1 invocation por evento no bus) e o Direct PUT tem quota por stream; na janela de
# medição de carga a regra fica desligada (var.ativa).

locals {
  nome_bucket = "${var.prefixo_bucket}-${var.account_id}"

  # Partição Hive (tipo=/ano=/mes=/dia=) que o Athena/Glue lê sem crawler (partition projection). `tipo` sai do
  # `detail-type` do envelope do EventBridge (dynamic partitioning); a data é a de chegada no Firehose, em UTC.
  prefixo_eventos = "eventos/"
  prefixo_erros   = "erros/"
  prefixo_s3      = "${local.prefixo_eventos}tipo=!{partitionKeyFromQuery:tipo}/ano=!{timestamp:yyyy}/mes=!{timestamp:MM}/dia=!{timestamp:dd}/"
  prefixo_s3_erro = "${local.prefixo_erros}!{firehose:error-output-type}/ano=!{timestamp:yyyy}/mes=!{timestamp:MM}/dia=!{timestamp:dd}/"
}

# ---------------------------------------------------------------- bucket de destino

# Bucket da data platform. force_destroy só no sandbox (o destroy precisa apagar os objetos versionados).
resource "aws_s3_bucket" "this" {
  #checkov:skip=CKV_AWS_18:Sandbox sem server access log (custo de um bucket de log a mais); só o Firehose grava aqui e o CloudTrail registra o plano de controle.
  #checkov:skip=CKV_AWS_144:Sem replicação entre regiões no sandbox: dado descartável, expira em 30 dias.
  #checkov:skip=CKV2_AWS_62:Ninguém consome notificação de objeto novo: o leitor (Athena/Glue) consulta por partição.
  bucket        = local.nome_bucket
  force_destroy = var.forcar_destruicao
}

# ACLs desligadas: o dono do bucket é dono de todo objeto, inclusive os gravados pelo Firehose.
resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Block Public Access completo no bucket.
resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versionamento: sobrescrita ou exclusão de objeto deixa a versão anterior (pré-requisito do Object Lock da produção).
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-KMS com a chave gerenciada pela AWS (aws/s3) e S3 Bucket Key. Sandbox: sem custo de chave, sem key policy para
# manter, e a política da chave gerenciada já autoriza quem grava pelo S3 nesta conta (mesmo desvio das tabelas no
# módulo dados). Produção: CMK dedicada, para compartilhar com a conta de dados (a aws/s3 não serve fora da conta),
# revogar e segregar quem decifra.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# Sandbox: tudo expira em var.retencao_dias (versões não correntes em 1 dia) e upload multipart abandonado sai em 1 dia.
# Produção troca esta regra por Object Lock COMPLIANCE com a retenção regulatória (10 anos, ADR-0017).
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expira-${var.retencao_dias}d"
    status = "Enabled"

    filter {}

    expiration {
      days = var.retencao_dias
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

# Só TLS: qualquer requisição sem aws:SecureTransport é negada, para qualquer principal.
# Depende do Block Public Access: as duas chamadas no mesmo bucket ao mesmo tempo podem ser recusadas pelo S3.
resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "SoTls"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}

# ---------------------------------------------------------------- Firehose (Direct PUT)

# Log de erro de entrega do Firehose (S3 negado, partição inválida). Só recebe linha quando algo falha.
resource "aws_cloudwatch_log_group" "firehose" {
  #checkov:skip=CKV_AWS_338:Retenção de 7 dias é desvio consciente do sandbox (custo); produção >= 365 dias.
  #checkov:skip=CKV_AWS_158:Sandbox usa a criptografia padrão do CloudWatch Logs; produção usa CMK.
  name              = "/aws/kinesisfirehose/${var.nome}"
  retention_in_days = var.retencao_logs_dias
}

# Stream de log onde o Firehose grava as falhas de entrega no S3 (nome convencional do console).
resource "aws_cloudwatch_log_stream" "entrega" {
  name           = "DestinationDelivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

# Role que o Firehose assume para gravar no bucket. sts:ExternalId = conta, como na trust policy documentada:
# https://docs.aws.amazon.com/firehose/latest/dev/controlling-access.html
resource "aws_iam_role" "firehose" {
  name                 = "desafio-firehose-${var.nome}"
  permissions_boundary = var.limite_de_permissoes_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "sts:ExternalId" = var.account_id } }
    }]
  })
}

# Menor privilégio: só PutObject nos dois prefixos do bucket (o upload multipart também é autorizado por PutObject) e
# PutLogEvents no stream de erro. A política de exemplo da AWS inclui leitura e listagem do bucket (GetObject,
# ListBucket, GetBucketLocation, ListBucketMultipartUploads) e AbortMultipartUpload: ficam de fora porque o Firehose só
# grava aqui, e o upload multipart abandonado é limpo pelo lifecycle. KMS só pela chamada do S3 (kms:ViaService): a key
# policy da aws/s3 já autoriza os principais da conta por esse caminho, mas a role tem permissions boundary, e a
# permissão explícita (com o mesmo teto no boundary) não depende de como a AWS combina os dois.
resource "aws_iam_role_policy" "firehose" {
  name = "gravar-no-bucket"
  role = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "GravarEventosEErros"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.this.arn}/${local.prefixo_eventos}*", "${aws_s3_bucket.this.arn}/${local.prefixo_erros}*"]
      },
      {
        Sid      = "LogDeErro"
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.firehose.arn}:log-stream:${aws_cloudwatch_log_stream.entrega.name}"]
      },
      {
        Sid       = "CifrarPeloS3"
        Effect    = "Allow"
        Action    = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "s3.sa-east-1.amazonaws.com" } }
      },
    ]
  })
}

# Stream Direct PUT (sem Kinesis na frente): o EventBridge grava direto nele. Buffer de 60 s para a demo mostrar o
# objeto no S3 em ~1 min; 64 MiB é o mínimo com dynamic partitioning. Cada registro é o evento do EventBridge inteiro
# (envelope + CloudEvent em `detail`), um por linha (AppendDelimiterToRecord), em GZIP.
resource "aws_kinesis_firehose_delivery_stream" "this" {
  #checkov:skip=CKV_AWS_241:Buffer do Firehose cifrado com chave da AWS (AWS_OWNED_CMK) no sandbox; produção usa CMK, junto da do bucket.
  name        = var.nome
  destination = "extended_s3"

  server_side_encryption {
    enabled  = true
    key_type = "AWS_OWNED_CMK"
  }

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.this.arn
    buffering_interval  = var.buffer_segundos
    buffering_size      = var.buffer_mib
    compression_format  = "GZIP"
    prefix              = local.prefixo_s3
    error_output_prefix = local.prefixo_s3_erro

    dynamic_partitioning_configuration {
      enabled        = true
      retry_duration = 300
    }

    processing_configuration {
      enabled = true

      # Chave de partição `tipo` = detail-type do evento (JQ sobre o registro JSON).
      processors {
        type = "MetadataExtraction"

        parameters {
          parameter_name  = "MetadataExtractionQuery"
          parameter_value = "{tipo: .[\"detail-type\"]}"
        }

        parameters {
          parameter_name  = "JsonParsingEngine"
          parameter_value = "JQ-1.6"
        }
      }

      # Uma linha por evento (NDJSON): sem isso os eventos saem colados no objeto e o Athena não separa.
      processors {
        type = "AppendDelimiterToRecord"
      }
    }

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.entrega.name
    }
  }

  # O Firehose valida a role na criação: a política precisa existir antes.
  depends_on = [aws_iam_role_policy.firehose]
}

# ---------------------------------------------------------------- assinatura no bus

# Regra da data platform: TODO evento de domínio dos sources do bus, sem filtro de detail-type. Tipo novo publicado
# pelo relay ou pelo CDC chega ao S3 sem mudar esta regra (o catálogo dos tipos é o schema registry).
resource "aws_cloudwatch_event_rule" "this" {
  name           = "para-${var.nome_consumidor}"
  event_bus_name = var.bus_nome
  state          = var.ativa ? "ENABLED" : "DISABLED"

  event_pattern = jsonencode({
    source = var.sources
  })
}

# Role que o EventBridge assume para entregar no Firehose, presa à regra (aws:SourceArn) e à conta (aws:SourceAccount).
resource "aws_iam_role" "eventbridge" {
  name                 = "desafio-eventbridge-${var.nome}"
  permissions_boundary = var.limite_de_permissoes_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.this.arn }
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

# Só as duas ações de escrita do stream, só neste stream.
resource "aws_iam_role_policy" "eventbridge" {
  name = "gravar-no-firehose"
  role = aws_iam_role.eventbridge.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "GravarNoStream"
      Effect   = "Allow"
      Action   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
      Resource = [aws_kinesis_firehose_delivery_stream.this.arn]
    }]
  })
}

# Target: a regra entrega no Firehose com a mesma política de retry dos outros targets do bus e a mesma DLQ de entrega
# (módulo eventos). Throttling ou permissão negada no Firehose vão para a DLQ com RULE_ARN/TARGET_ARN/ERROR_CODE (R1).
resource "aws_cloudwatch_event_target" "this" {
  rule           = aws_cloudwatch_event_rule.this.name
  event_bus_name = var.bus_nome
  target_id      = var.nome_consumidor
  arn            = aws_kinesis_firehose_delivery_stream.this.arn
  role_arn       = aws_iam_role.eventbridge.arn

  retry_policy {
    maximum_event_age_in_seconds = var.idade_maxima_entrega_segundos
    maximum_retry_attempts       = var.tentativas_maximas_entrega
  }

  dead_letter_config {
    arn = var.dlq_de_entrega_arn
  }
}

# ---------------------------------------------------------------- alarme

# Defasagem da entrega no S3: idade do registro mais antigo ainda no Firehose. Com buffer de 60 s o normal fica perto
# de 1–2 min; passar do limiar é S3 negado (role, boundary, bucket policy) ou partição falhando. Sem tráfego não há
# ponto, e o alarme fica OK (notBreaching).
resource "aws_cloudwatch_metric_alarm" "defasagem" {
  alarm_name          = "${var.prefixo_alarmes}-${var.nome_consumidor}-defasagem"
  alarm_description   = "O Firehose ${var.nome} tem registro esperando há mais de ${var.defasagem_limite_segundos} s para chegar ao S3 (entrega falhando). Runbook R15."
  namespace           = "AWS/Firehose"
  metric_name         = "DeliveryToS3.DataFreshness"
  dimensions          = { DeliveryStreamName = aws_kinesis_firehose_delivery_stream.this.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.defasagem_limite_segundos
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme
}
