locals {
  api_dim = ["ApiName", var.api_nome, "Stage", var.api_stage]
  ops_ddb = ["GetItem", "PutItem", "UpdateItem"]
  # Todo alarme notifica o tópico de alarmes (ALARM e de volta a OK), mais o que vier de fora em var.acoes_alarme.
  acoes = concat([aws_sns_topic.alarmes.arn], var.acoes_alarme)
}

# ---------------------------------------------------------------- notificação dos alarmes (SNS)

# Chave KMS do tópico de alarmes. NÃO é a gerenciada alias/aws/sns: a política dela não deixa o CloudWatch usar a
# chave, e o alarme falharia ao publicar num tópico criptografado com ela. Para serviços AWS publicarem num tópico
# criptografado a AWS exige chave gerenciada pelo cliente com kms:GenerateDataKey* e kms:Decrypt para o serviço:
# https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html#compatibility-with-aws-services
# Custo: ~US$ 1/mês pela chave, mais requisições (só quando um alarme muda de estado).
resource "aws_kms_key" "alarmes" {
  description             = "SNS ${var.nome}-alarmes: notificacoes dos alarmes do CloudWatch"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Administração pela própria conta via IAM (padrão da AWS; sem isto a chave fica inadministrável).
        Sid       = "AdministracaoPelaConta"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # O CloudWatch cifra a notificação ao publicar. Sem aws:SourceAccount/SourceArn aqui: a documentação do SNS
        # manda consultar a do serviço, e a do CloudWatch só documenta essas condições na política do TÓPICO (abaixo).
        Sid       = "CloudWatchPublicaNoTopico"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
      },
    ]
  })
}

# Alias legível da chave dos alarmes.
resource "aws_kms_alias" "alarmes" {
  name          = "alias/${var.nome}-alarmes"
  target_key_id = aws_kms_key.alarmes.key_id
}

# Tópico de notificação de todos os alarmes da stack (plantão). Criptografado com a chave acima.
resource "aws_sns_topic" "alarmes" {
  name              = "${var.nome}-alarmes"
  kms_master_key_id = aws_kms_key.alarmes.arn
}

# Só o CloudWatch, a partir de alarmes desta conta e região, publica no tópico (anti-confused-deputy):
# https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html#SNS_Confused_Deputy
# Pessoas e roles da conta continuam administrando o tópico pelas políticas IAM delas (mesma conta).
resource "aws_sns_topic_policy" "alarmes" {
  arn = aws_sns_topic.alarmes.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AlarmesDaContaPublicam"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alarmes.arn
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.regiao}:${var.account_id}:alarm:*" }
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

# Assinatura de e-mail opcional (vazio = sem assinatura). A AWS manda um e-mail de confirmação; até confirmar, nada chega.
resource "aws_sns_topic_subscription" "email" {
  count = var.email_alarmes == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarmes.arn
  protocol  = "email"
  endpoint  = var.email_alarmes
}

# Dashboard único: API, DynamoDB, ECS, SQS/DLQ e relay.
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.nome

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "API: Count, 4XX, 5XX"
          region = var.regiao, view = "timeSeries", stat = "Sum", period = 60
          metrics = [
            concat(["AWS/ApiGateway", "Count"], local.api_dim),
            concat(["AWS/ApiGateway", "4XXError"], local.api_dim),
            concat(["AWS/ApiGateway", "5XXError"], local.api_dim),
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "API: latência p50 / p99 e IntegrationLatency p99 (ms)"
          region = var.regiao, view = "timeSeries", period = 60
          metrics = [
            concat(["AWS/ApiGateway", "Latency"], local.api_dim, [{ stat = "p50", label = "Latency p50" }]),
            concat(["AWS/ApiGateway", "Latency"], local.api_dim, [{ stat = "p99", label = "Latency p99" }]),
            concat(["AWS/ApiGateway", "IntegrationLatency"], local.api_dim, [{ stat = "p99", label = "IntegrationLatency p99" }]),
          ]
          annotations = { horizontal = [{ label = "SLO p99 (ADR-0001)", value = var.p99_limite_ms }, { label = "alarme", value = var.p99_alarme_ms }] }
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "DynamoDB: throttling (leitura + escrita)"
          region = var.regiao, view = "timeSeries", stat = "Sum", period = 60
          # setproduct (e não flatten): o dashboard exige lista de LISTAS; flatten achatava tudo em strings.
          metrics = [
            for par in setproduct(var.tabelas, ["ReadThrottleEvents", "WriteThrottleEvents"]) :
            ["AWS/DynamoDB", par[1], "TableName", par[0]]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "DynamoDB: SuccessfulRequestLatency (média, ms)"
          region = var.regiao, view = "timeSeries", stat = "Average", period = 60
          metrics = [
            for par in setproduct(var.tabelas_com_latencia, local.ops_ddb) :
            ["AWS/DynamoDB", "SuccessfulRequestLatency", "TableName", par[0], "Operation", par[1]]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "ECS: CPU (%)"
          region = var.regiao, view = "timeSeries", stat = "Average", period = 60
          metrics = [
            for s in var.servicos_ecs : ["AWS/ECS", "CPUUtilization", "ClusterName", var.cluster_nome, "ServiceName", s]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "ECS: memória (%)"
          region = var.regiao, view = "timeSeries", stat = "Average", period = 60
          metrics = [
            for s in var.servicos_ecs : ["AWS/ECS", "MemoryUtilization", "ClusterName", var.cluster_nome, "ServiceName", s]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 18, width = 12, height = 6
        properties = {
          title  = "SQS: idade da mensagem mais antiga (s)"
          region = var.regiao, view = "timeSeries", stat = "Maximum", period = 60
          metrics = [
            for q in var.filas : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", q]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 18, width = 12, height = 6
        properties = {
          title  = "DLQs: mensagens visíveis (deve ser 0)"
          region = var.regiao, view = "timeSeries", stat = "Maximum", period = 60
          metrics = [
            for q in var.dlqs : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 24, width = 12, height = 6
        properties = {
          title  = "Relay: erros e invocações"
          region = var.regiao, view = "timeSeries", stat = "Sum", period = 60
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", var.relay_funcao],
            ["AWS/Lambda", "Invocations", "FunctionName", var.relay_funcao],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 24, width = 12, height = 6
        properties = {
          title       = "Relay: IteratorAge (ms)"
          region      = var.regiao, view = "timeSeries", stat = "Maximum", period = 60
          metrics     = [["AWS/Lambda", "IteratorAge", "FunctionName", var.relay_funcao]]
          annotations = { horizontal = [{ label = "alarme", value = var.iterator_age_limite_ms }] }
        }
      },
      {
        type = "metric", x = 0, y = 30, width = 12, height = 6
        properties = {
          title       = "Limites: operações na guarda de idempotência (máx. por contrato)"
          region      = var.regiao, view = "timeSeries", stat = "Maximum", period = 300
          metrics     = [["desafio-zup/limites", "OperacoesNaGuarda"]]
          annotations = { horizontal = [{ label = "alarme", value = var.limite_guarda_operacoes }] }
        }
      },
      {
        type = "metric", x = 12, y = 30, width = 12, height = 6
        properties = {
          title       = "Limites: reservas ativas no item (máx. por contrato)"
          region      = var.regiao, view = "timeSeries", stat = "Maximum", period = 300
          metrics     = [["desafio-zup/limites", "ReservasAtivas"]]
          annotations = { horizontal = [{ label = "alarme", value = var.limite_reservas_ativas }] }
        }
      },
    ]
  })
}

# Disponibilidade por burn rate (SRE Workbook, cap. 5, "multiwindow, multi-burn-rate alerts") sobre o SLO de 99,9% ao
# mês (ADR-0001): orçamento de erro de 0,1% das requisições. Burn rate N = gastar o orçamento N vezes mais rápido do que
# o mês permite. Dois pares de janelas; cada par só dispara se a janela longa E a curta passarem do limiar (a longa dá
# significância, a curta faz o alarme voltar a OK logo que o problema acaba):
#   rápido: 1 h e 5 min acima de 14,4x (1,44% de erro) = 2% do orçamento do mês em 1 h;
#   lento:  6 h e 30 min acima de 6x (0,6% de erro)    = 5% do orçamento do mês em 6 h.
# Erro = 5XXError do stage, o que INCLUI o 503 do load shedding e o 504 de timeout da integração. É de propósito: o
# shedding protege a frota, mas o canal recebeu erro e isso consome o orçamento (o ADR-0013 chama o 503 de comportamento
# projetado; projetado não quer dizer de graça). 4XX, inclusive o 429 do WAF e do usage plan, não entra.
# Janela com média abaixo de var.burn_rate_rps_minimo não avalia: no sandbox ocioso, 1 erro em 20 requisições do smoke
# daria 5% e "queimaria" um orçamento sem amostra para existir. A 5.000 TPS a guarda nunca age.
locals {
  orcamento_de_erro_pct = (1 - var.slo_disponibilidade) * 100

  janelas_de_burn = {
    rapido-1h   = { par = "rapido", periodo = 3600, fator = 14.4 }
    rapido-5min = { par = "rapido", periodo = 300, fator = 14.4 }
    lento-6h    = { par = "lento", periodo = 21600, fator = 6 }
    lento-30min = { par = "lento", periodo = 1800, fator = 6 }
  }

  pares_de_burn = {
    rapido = { longa = "rapido-1h", curta = "rapido-5min", descricao = "1 h e 5 min acima de 14,4x o orçamento (2% do orçamento do mês em 1 h)" }
    lento  = { longa = "lento-6h", curta = "lento-30min", descricao = "6 h e 30 min acima de 6x o orçamento (5% do orçamento do mês em 6 h)" }
  }
}

# Uma janela do burn rate: % de 5XX sobre o total da janela, contra fator x orçamento. Sem ação própria: quem notifica é
# o alarme composto do par (abaixo); a janela curta sozinha dispararia num pico de segundos.
resource "aws_cloudwatch_metric_alarm" "api_5xx_janela" {
  for_each = local.janelas_de_burn

  alarm_name          = "${var.nome}-api-5xx-burn-${each.key}"
  alarm_description   = "Janela ${each.key} do burn rate de 5XX (inclui o 503 do shedding): taxa acima de ${each.value.fator}x o orçamento de ${format("%.2f", local.orcamento_de_erro_pct)}%. Notifica pelo composto api-5xx-burn-${each.value.par}."
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.fator * local.orcamento_de_erro_pct
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "taxa"
    expression  = "IF(total >= ${var.burn_rate_rps_minimo * each.value.periodo}, 100 * FILL(erros, 0) / total, 0)"
    label       = "Taxa de 5XX (%) em ${each.key}"
    return_data = true
  }

  metric_query {
    id = "erros"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5XXError"
      period      = each.value.periodo
      stat        = "Sum"
      dimensions  = { ApiName = var.api_nome, Stage = var.api_stage }
    }
  }

  metric_query {
    id = "total"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      period      = each.value.periodo
      stat        = "Sum"
      dimensions  = { ApiName = var.api_nome, Stage = var.api_stage }
    }
  }
}

# Alarme composto por par de janelas (longa E curta): é este que notifica o plantão.
resource "aws_cloudwatch_composite_alarm" "api_5xx_burn" {
  for_each = local.pares_de_burn

  alarm_name        = "${var.nome}-api-5xx-burn-${each.key}"
  alarm_description = "SLO de disponibilidade (${format("%.2f", var.slo_disponibilidade * 100)}%) em risco: 5XX, inclusive o 503 do shedding, ${each.value.descricao}. Runbook R10."
  alarm_rule        = "ALARM(\"${aws_cloudwatch_metric_alarm.api_5xx_janela[each.value.longa].alarm_name}\") AND ALARM(\"${aws_cloudwatch_metric_alarm.api_5xx_janela[each.value.curta].alarm_name}\")"
  alarm_actions     = local.acoes
  ok_actions        = local.acoes
}

# Alarme de latência p99 da API, 3 de 5 minutos. O limiar NÃO é o SLO: o SLO de p99 <= 150 ms (ADR-0001) é a linha do
# dashboard, e o entregue não o atinge (164 ms no melhor degrau, 526 ms a 60% de CPU, 874 ms saturado com shedding:
# docs/capacidade.md, seção 3). Alarmar em 150 ms deixaria o alarme em ALARM o tempo todo. var.p99_alarme_ms (400 ms,
# ~2,7x o SLO) separa a lacuna conhecida de "o serviço piorou": dispara quando a frota roda sem folga de CPU ou uma
# dependência fica lenta. Fechar o SLO é a alavanca 1 de docs/capacidade.md, não este alarme. Runbook R11.
resource "aws_cloudwatch_metric_alarm" "api_p99" {
  alarm_name          = "${var.nome}-api-latencia-p99"
  alarm_description   = "Latência p99 da API acima de ${var.p99_alarme_ms} ms em 3 de 5 min (o SLO é ${var.p99_limite_ms} ms, ADR-0001; o limiar fica acima do medido com folga de CPU). Runbook R11."
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  dimensions          = { ApiName = var.api_nome, Stage = var.api_stage }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.p99_alarme_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Alarme por DLQ: qualquer mensagem visível significa falha de processamento que exige olhar humano.
resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each = toset(var.dlqs)

  alarm_name          = "${var.nome}-dlq-${each.value}"
  alarm_description   = "Há mensagens na DLQ ${each.value}."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Alarme: relay atrasado (IteratorAge > 60 s) = eventos do outbox demorando a sair.
resource "aws_cloudwatch_metric_alarm" "relay_iterator_age" {
  alarm_name          = "${var.nome}-relay-iterator-age"
  alarm_description   = "O relay está mais de 60 s atrás do stream do outbox."
  namespace           = "AWS/Lambda"
  metric_name         = "IteratorAge"
  dimensions          = { FunctionName = var.relay_funcao }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.iterator_age_limite_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Alarme: CDC de limites atrasado (IteratorAge > 60 s) = LimiteAlterado demorando a sair do Stream da tabela limites.
resource "aws_cloudwatch_metric_alarm" "cdc_iterator_age" {
  alarm_name          = "${var.nome}-relay-cdc-limites-iterator-age"
  alarm_description   = "O relay-cdc-limites está mais de ${var.iterator_age_limite_ms / 1000} s atrás do Stream da tabela limites (LimiteAlterado atrasado). Runbook R12."
  namespace           = "AWS/Lambda"
  metric_name         = "IteratorAge"
  dimensions          = { FunctionName = var.cdc_funcao }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.iterator_age_limite_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Alarme por função Lambda: invocação que terminou em erro (exceção não tratada, timeout, falta de memória). Falha
# PARCIAL de lote (ReportBatchItemFailures) não conta como Errors: essa aparece no IteratorAge e na DLQ.
resource "aws_cloudwatch_metric_alarm" "lambda_erros" {
  for_each = var.funcoes_lambda

  alarm_name          = "${var.nome}-lambda-erros-${each.key}"
  alarm_description   = "A função ${each.value} terminou invocação com erro nos últimos 5 min (exceção, timeout ou memória). Runbook R12."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Alarme por fila consumidora: a mensagem mais antiga esperando mais que o limiar = consumidor parado ou mais lento que a
# chegada (o teto do ESM da contabilização, por exemplo). A DLQ não pega isso: o redrive é por recebimento, não por
# espera. A fila do legado fica de fora porque tem alarme próprio, com o limiar do ADR-0014 (defasagem_legado).
resource "aws_cloudwatch_metric_alarm" "idade_da_fila" {
  for_each = toset([for f in var.filas : f if f != var.fila_legado])

  alarm_name          = "${var.nome}-idade-da-fila-${each.value}"
  alarm_description   = "A mensagem mais antiga da fila ${each.value} espera há mais de ${var.idade_fila_limite_s} s: consumidor parado ou mais lento que a chegada. Runbook R13."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.idade_fila_limite_s
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Sinais de log sem métrica nativa: um alarme por sinal, alimentado por um ou mais metric filters (mesma métrica).
# O padrão segue o formato de cada log group: no /ecs/autorizacao a linha é JSON puro (seletor JSON); no
# /ecs/reconciliador ela vem com prefixo de data e logger, e no Lambda (log_format Text) com nível e request id, então
# lá o filtro casa o TERMO, como os do reconciliador. Filtro sem linha correspondente é inofensivo: a métrica fica em 0.
locals {
  alarmes_de_sinal = {
    # O limites devolveu um motivo de recusa que o autorizacao não conhece: o leitor tolerante mapeia para
    # RECUSA_DO_LIMITE, mas o contrato interno mudou sem o cliente acompanhar (painel 6, E-N6).
    motivo-desconhecido-do-limite = {
      namespace = "desafio-zup/autorizacao"
      metrica   = "MotivoDesconhecidoDoLimite"
      descricao = "O autorizacao (API ou reconciliador) recebeu do limites um motivo de recusa fora do enum que conhece (vira RECUSA_DO_LIMITE). Runbook R14."
    }
    # Registro do Stream de limites que o CDC descartou sem publicar LimiteAlterado (painel 6, F-6).
    cdc-registro-descartado = {
      namespace = "desafio-zup/relay"
      metrica   = "RegistrosDoCdcDescartados"
      descricao = "O relay-cdc-limites descartou registro do Stream de limites (LimiteAlterado não publicado). Runbook R14."
    }
  }

  filtros_de_sinal = {
    motivo-desconhecido-autorizacao = {
      alarme    = "motivo-desconhecido-do-limite"
      log_group = var.log_group_autorizacao
      padrao    = "{ $.evento = \"motivo_desconhecido_do_limite\" }"
    }
    motivo-desconhecido-reconciliador = {
      alarme    = "motivo-desconhecido-do-limite"
      log_group = var.log_group_reconciliador
      padrao    = "\"motivo_desconhecido_do_limite\""
    }
    cdc-registro-descartado = {
      alarme    = "cdc-registro-descartado"
      log_group = var.log_group_cdc
      padrao    = "\"cdc_registro_descartado\""
    }
  }
}

# Um metric filter por (sinal, log group): cada linha que casa vale 1 na métrica do sinal.
resource "aws_cloudwatch_log_metric_filter" "sinal" {
  for_each       = local.filtros_de_sinal
  name           = "${var.nome}-${each.key}"
  log_group_name = each.value.log_group
  pattern        = each.value.padrao

  metric_transformation {
    name          = local.alarmes_de_sinal[each.value.alarme].metrica
    namespace     = local.alarmes_de_sinal[each.value.alarme].namespace
    value         = "1"
    default_value = "0"
  }
}

# Um alarme por sinal de log: uma ocorrência em 5 min basta (não deveria acontecer nunca).
resource "aws_cloudwatch_metric_alarm" "sinal" {
  for_each            = local.alarmes_de_sinal
  alarm_name          = "${var.nome}-${each.key}"
  alarm_description   = each.value.descricao
  namespace           = each.value.namespace
  metric_name         = each.value.metrica
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes

  depends_on = [aws_cloudwatch_log_metric_filter.sinal]
}

# Métrica a partir do log JSON do limites: uma linha por escrita de contrato com guarda OU reservas >= 200 entradas.
resource "aws_cloudwatch_log_metric_filter" "guarda" {
  name           = "${var.nome}-operacoes-na-guarda"
  log_group_name = var.log_group_limites
  pattern        = "{ $.evento = \"guarda_de_idempotencia\" }"

  metric_transformation {
    name      = "OperacoesNaGuarda"
    namespace = "desafio-zup/limites"
    value     = "$.operacoes_na_guarda"
    unit      = "Count"
  }
}

# Alarme: guarda de algum contrato acima do limiar. ~2000 entradas x ~45 bytes = ~90 KB = ~88 WCU por escrita; com a janela
# de 5 min isso é ~7 op/s sustentadas, ~60% dos 1.000 WCU/s da partição. Gatilho do ADR-0016 para fragmentar o contrato.
resource "aws_cloudwatch_metric_alarm" "guarda" {
  alarm_name          = "${var.nome}-guarda-de-idempotencia"
  alarm_description   = "A guarda de idempotência de um contrato passou de ${var.limite_guarda_operacoes} operações (gatilho do ADR-0016)."
  namespace           = "desafio-zup/limites"
  metric_name         = "OperacoesNaGuarda"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.limite_guarda_operacoes
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes

  depends_on = [aws_cloudwatch_log_metric_filter.guarda]
}

# Mesma linha JSON, outro campo: reservas ativas no item. Holds vivem até 7 dias DENTRO do item do limite (a guarda vive
# 5 min), então um contrato com muitas pré-autorizações pendentes cresce sem a guarda acusar (ADR-0016).
resource "aws_cloudwatch_log_metric_filter" "reservas" {
  name           = "${var.nome}-reservas-ativas"
  log_group_name = var.log_group_limites
  pattern        = "{ $.evento = \"guarda_de_idempotencia\" }"

  metric_transformation {
    name      = "ReservasAtivas"
    namespace = "desafio-zup/limites"
    value     = "$.reservas_ativas"
    unit      = "Count"
  }
}

# Alarme: reservas ativas de algum contrato acima do limiar. ~2.000 reservas x ~45 bytes = ~90 KB no item: cada escrita
# já custa ~90 WCU, e ~9.000 reservas encostam nos 400 KB (aí a escrita falha com ValidationException e o contrato para).
resource "aws_cloudwatch_metric_alarm" "reservas" {
  alarm_name          = "${var.nome}-reservas-ativas-no-item"
  alarm_description   = "Um contrato passou de ${var.limite_reservas_ativas} reservas ativas no item do limite (holds de até 7 dias; teto do item 400 KB, ADR-0016)."
  namespace           = "desafio-zup/limites"
  metric_name         = "ReservasAtivas"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.limite_reservas_ativas
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes

  depends_on = [aws_cloudwatch_log_metric_filter.reservas]
}

# Alarme: defasagem do feed do core legado (ADR-0014). A mensagem mais antiga da fila legado-contratos esperando mais
# que o limiar (5 min) = o limite concedido na réplica está velho por isso. É o controle que limita o risco aceito no
# ADR-0014 (autorizar com o concedido antigo).
resource "aws_cloudwatch_metric_alarm" "defasagem_legado" {
  alarm_name          = "${var.nome}-defasagem-do-legado"
  alarm_description   = "A mensagem mais antiga da fila ${var.fila_legado} espera há mais de ${var.defasagem_legado_limite_s} s: a réplica do limite concedido está defasada (ADR-0014)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = var.fila_legado }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.defasagem_legado_limite_s
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes
}

# Contributor Insights sobre o mesmo log: top contratos por tamanho de guarda (quem está perto de precisar fragmentar).
resource "aws_cloudwatch_contributor_insight_rule" "guarda" {
  rule_name  = "${var.nome}-guarda-por-contrato"
  rule_state = "ENABLED"
  rule_definition = jsonencode({
    Schema        = { Name = "CloudWatchLogRule", Version = 1 }
    LogGroupNames = [var.log_group_limites]
    LogFormat     = "JSON"
    Contribution = {
      Keys    = ["$.id_contrato"]
      ValueOf = "$.operacoes_na_guarda"
      Filters = [{ Match = "$.evento", In = ["guarda_de_idempotencia"] }]
    }
    AggregateOn = "Sum"
  })
}

# Alarmes do reconciliador/expirador. A linha de rodada não é JSON puro (tem prefixo de data e logger), então o filtro casa
# o TERMO da contagem: cada linha que contém o termo vale 1 (uma rodada com o desfecho), a cada 30 s.
locals {
  alarmes_do_reconciliador = {
    # Órfã ou reserva sem hold saiu do caminho automático para a fila humana (RM#): alguém precisa decidir (R7).
    encaminhadas = {
      padrao    = "?\"orfa_ENCAMINHADA\" ?\"reserva_ENCAMINHADA\""
      metrica   = "OrfasEncaminhadas"
      periodo   = 300
      limiar    = 1
      descricao = "Autorização ou reserva encaminhada para reconciliação manual (fila RM#). Ver runbook R7."
    }
    # Falha repetida: limites fora, contrato sumido ou RESERVA_DIVERGENTE. 6 rodadas em 5 min = ~3 min falhando.
    falhas = {
      padrao    = "?\"orfa_FALHOU\" ?\"reserva_FALHOU\""
      metrica   = "FalhasDoReconciliador"
      periodo   = 300
      limiar    = 6
      descricao = "Reconciliador ou expirador falhando há ~3 min (limites fora, contrato ausente ou hold divergente)."
    }
    # Disputa pelo hold se resolve em segundos; 20 rodadas em 15 min = expiração interrompida por mais de 5 min.
    hold_perdido = {
      padrao    = "\"reserva_PERDEU_O_HOLD\""
      metrica   = "ReservasSemHold"
      periodo   = 900
      limiar    = 20
      descricao = "Reserva vencida segue APROVADA sem hold por 10+ min: expiração interrompida. Reconciliação manual."
    }
    # Varredura que LEVANTOU (exceção, não desfecho de negócio): a linha da rodada só traz o campo
    # varreduras_com_erro quando alguma das duas varreduras falhou. O heartbeat só renova em rodada sem exceção e o
    # healthcheck reprova com ele acima de 90 s: 2 rodadas com erro em 2 min (de ~4) = a task está a uma rodada de
    # ser reciclada pelo ECS. Um soluço isolado não dispara.
    varreduras_com_erro = {
      padrao    = "\"varreduras_com_erro\""
      metrica   = "RodadasComVarreduraComErro"
      periodo   = 120
      limiar    = 2
      descricao = "Rodadas do reconciliador com varredura que levantou exceção (2+ em 2 min): heartbeat sem renovar, task perto de ser reciclada. Runbook R8."
    }
  }
}

# Um metric filter por desfecho do reconciliador.
resource "aws_cloudwatch_log_metric_filter" "reconciliador" {
  for_each       = local.alarmes_do_reconciliador
  name           = "${var.nome}-reconciliador-${each.key}"
  log_group_name = var.log_group_reconciliador
  pattern        = each.value.padrao

  metric_transformation {
    name          = each.value.metrica
    namespace     = "desafio-zup/autorizacao"
    value         = "1"
    default_value = "0"
  }
}

# Um alarme por desfecho, sobre a soma de rodadas no período.
resource "aws_cloudwatch_metric_alarm" "reconciliador" {
  for_each            = local.alarmes_do_reconciliador
  alarm_name          = "${var.nome}-reconciliador-${each.key}"
  alarm_description   = each.value.descricao
  namespace           = "desafio-zup/autorizacao"
  metric_name         = each.value.metrica
  statistic           = "Sum"
  period              = each.value.periodo
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = each.value.limiar
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.acoes
  ok_actions          = local.acoes

  depends_on = [aws_cloudwatch_log_metric_filter.reconciliador]
}
