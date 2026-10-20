locals {
  api_dim = ["ApiName", var.api_nome, "Stage", var.api_stage]
  ops_ddb = ["GetItem", "PutItem", "UpdateItem"]
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
          annotations = { horizontal = [{ label = "RNF p99", value = var.p99_limite_ms }] }
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
    ]
  })
}

# Alarme: taxa de 5XX acima de 1% das requisições em 5 minutos (métrica calculada 5XX/Count).
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.nome}-api-5xx-acima-de-1pct"
  alarm_description   = "Mais de 1% das requisições da API terminaram em 5XX nos últimos 5 minutos."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme

  metric_query {
    id          = "taxa"
    expression  = "100 * erros / FILL(total, 1)"
    label       = "Taxa de 5XX (%)"
    return_data = true
  }

  metric_query {
    id = "erros"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5XXError"
      period      = 300
      stat        = "Sum"
      dimensions  = { ApiName = var.api_nome, Stage = var.api_stage }
    }
  }

  metric_query {
    id = "total"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      period      = 300
      stat        = "Sum"
      dimensions  = { ApiName = var.api_nome, Stage = var.api_stage }
    }
  }
}

# Alarme: latência p99 da API acima de 150 ms (RNF), 3 de 5 minutos.
resource "aws_cloudwatch_metric_alarm" "api_p99" {
  alarm_name          = "${var.nome}-api-latencia-p99"
  alarm_description   = "Latência p99 da API acima do RNF (150 ms)."
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  dimensions          = { ApiName = var.api_nome, Stage = var.api_stage }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.p99_limite_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme
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
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme
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
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme
}

# Métrica a partir do log JSON do limites: uma linha por contrato cuja guarda passou de 200 entradas.
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
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme

  depends_on = [aws_cloudwatch_log_metric_filter.guarda]
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
  alarm_actions       = var.acoes_alarme
  ok_actions          = var.acoes_alarme

  depends_on = [aws_cloudwatch_log_metric_filter.reconciliador]
}
