# Testes do ambiente sandbox com provider mockado: só `plan`, nenhuma chamada à AWS.
# Execução: make tf-test. Diretório real dentro do ambiente (o terraform test exige o diretório de testes DENTRO do
# diretório da configuração; o symlink que existia antes virava arquivo no unzip do Windows).
# Cada run carrega o módulo que quer inspecionar: um teste de raiz só enxerga outputs, e atributos
# computados (ARNs) são desconhecidos no plan, o que impediria inspecionar as policies.

mock_provider "aws" {}

# --- Tabelas DynamoDB: todas com PITR (e as 5 esperadas) ---------------------------------------------
run "dados_com_pitr" {
  command = plan

  module {
    source = "../../modules/dados"
  }

  assert {
    condition     = length(aws_dynamodb_table.this) == 5
    error_message = "Esperadas 5 tabelas (limites, autorizacoes, autorizacoes-outbox, contabilizacao-lancamentos, contabilizacao-inbox)."
  }

  assert {
    condition     = alltrue([for t in aws_dynamodb_table.this : t.point_in_time_recovery[0].enabled])
    error_message = "Todas as tabelas DynamoDB devem ter PITR habilitado."
  }
}

# --- GSIs esparsos da tabela autorizacoes (órfãs ADR-0006; vencimentos) e Contributor Insights -----------
run "dados_com_gsi_de_orfas" {
  command = plan

  module {
    source = "../../modules/dados"
  }

  assert {
    condition     = toset(keys(aws_dynamodb_contributor_insights.this)) == toset(["autorizacoes", "limites"])
    error_message = "Contributor Insights deve estar habilitado em limites e autorizacoes."
  }

  assert {
    condition     = alltrue([for k, t in aws_dynamodb_table.this : length(t.global_secondary_index) == 0 if k != "autorizacoes"])
    error_message = "Só a tabela autorizacoes tem GSI."
  }
}

# --- Warm throughput: só existe quando configurado (sandbox desligado), na tabela e no GSI ------------------
run "dados_com_warm_throughput_configurado" {
  command = plan

  module {
    source = "../../modules/dados"
  }

  variables {
    warm_throughput = {
      limites      = { escrita = 11000 }
      autorizacoes = { escrita = 31000, indices = { gsi_orfas = { escrita = 20000 } } }
    }
  }

  assert {
    condition     = aws_dynamodb_table.this["limites"].warm_throughput[0].write_units_per_second == 11000
    error_message = "warm_throughput configurado deve chegar na tabela limites."
  }

  assert {
    condition = one([
      for g in aws_dynamodb_table.this["autorizacoes"].global_secondary_index : g.warm_throughput[0].write_units_per_second
      if g.name == "gsi_orfas"
    ]) == 20000
    error_message = "warm_throughput configurado deve chegar no GSI gsi_orfas."
  }
}

# --- Filas consumidoras com redrive para DLQ ---------------------------------------------------------
run "filas_com_redrive" {
  command = plan

  module {
    source = "../../modules/eventos"
  }

  override_resource {
    target          = aws_sqs_queue.dlq
    override_during = plan
    values = {
      arn = "arn:aws:sqs:sa-east-1:123456789012:dlq"
    }
  }

  assert {
    condition     = length(aws_sqs_queue.this) == 3
    error_message = "Esperadas 3 filas consumidoras (contabilizacao, antifraude, notificacoes)."
  }

  assert {
    condition = alltrue([
      for q in aws_sqs_queue.this :
      jsondecode(q.redrive_policy).maxReceiveCount == 5 && startswith(jsondecode(q.redrive_policy).deadLetterTargetArn, "arn:aws:sqs:")
    ])
    error_message = "Toda fila consumidora deve ter redrive_policy (maxReceiveCount 5) apontando para a DLQ."
  }

  # Feed do legado: fila + DLQ (maxReceiveCount 5) e regra LimiteAlterado -> notificacoes.
  override_resource {
    target          = aws_sqs_queue.legado_dlq
    override_during = plan
    values = {
      arn = "arn:aws:sqs:sa-east-1:123456789012:legado-contratos-dlq"
    }
  }

  assert {
    condition = (
      aws_sqs_queue.legado.name == "legado-contratos"
      && jsondecode(aws_sqs_queue.legado.redrive_policy).maxReceiveCount == 5
      && startswith(jsondecode(aws_sqs_queue.legado.redrive_policy).deadLetterTargetArn, "arn:aws:sqs:")
    )
    error_message = "A fila legado-contratos deve ter redrive_policy (maxReceiveCount 5) para a DLQ."
  }

  assert {
    condition = (
      contains(jsondecode(aws_cloudwatch_event_rule.limite_alterado.event_pattern).source, "financas.limites")
      && contains(jsondecode(aws_cloudwatch_event_rule.limite_alterado.event_pattern)["detail-type"], "LimiteAlterado")
      && aws_cloudwatch_event_target.limite_alterado.target_id == "notificacoes"
    )
    error_message = "LimiteAlterado (source financas.limites) deve ir para a fila notificacoes."
  }

  # Targets do EventBridge: DLQ de entrega (SSE) e retry explícito; a DLQ só aceita as regras do bus.
  override_resource {
    target          = aws_sqs_queue.entrega_dlq
    override_during = plan
    values = {
      arn = "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
    }
  }

  override_resource {
    target          = aws_cloudwatch_event_rule.this
    override_during = plan
    values = {
      arn = "arn:aws:events:sa-east-1:123456789012:rule/financas/para-x"
    }
  }

  override_resource {
    target          = aws_cloudwatch_event_rule.limite_alterado
    override_during = plan
    values = {
      arn = "arn:aws:events:sa-east-1:123456789012:rule/financas/limite-alterado-para-notificacoes"
    }
  }

  assert {
    condition = alltrue([
      for t in concat(values(aws_cloudwatch_event_target.this), [aws_cloudwatch_event_target.limite_alterado]) :
      t.dead_letter_config[0].arn == "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
      && t.retry_policy[0].maximum_event_age_in_seconds == 3600
      && t.retry_policy[0].maximum_retry_attempts == 185
    ])
    error_message = "Todo target do EventBridge deve ter DLQ de entrega e retry_policy explícita (1 h, 185 tentativas)."
  }

  assert {
    condition     = aws_sqs_queue.entrega_dlq.sqs_managed_sse_enabled == true
    error_message = "A DLQ de entrega deve ter SSE."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue_policy.entrega_dlq.policy).Statement[0].Principal.Service == "events.amazonaws.com"
      && contains(
        jsondecode(aws_sqs_queue_policy.entrega_dlq.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"],
        "arn:aws:events:sa-east-1:123456789012:rule/financas/limite-alterado-para-notificacoes"
      )
      && length(jsondecode(aws_sqs_queue_policy.entrega_dlq.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == 4
    )
    error_message = "A policy da DLQ de entrega deve aceitar só o EventBridge, a partir das 4 regras do bus (aws:SourceArn)."
  }
}

# --- API: tracing no stage e gateway responses em problem+json ---------------------------------------
run "api_com_tracing_e_problem_json" {
  command = plan

  module {
    source = "../../modules/api"
  }

  variables {
    account_id               = "123456789012"
    vpc_id                   = "vpc-00000000"
    subnet_ids               = ["subnet-00000001", "subnet-00000002"]
    contrato_openapi         = "../../../../contracts/openapi/autorizacao-v1.yaml"
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
  }

  assert {
    condition     = aws_api_gateway_stage.this.xray_tracing_enabled == true
    error_message = "O stage v1 deve ter tracing (X-Ray) habilitado."
  }

  assert {
    condition = alltrue([
      for k in ["THROTTLED", "DEFAULT_5XX"] :
      output.respostas_do_gateway[k].responseParameters["gatewayresponse.header.Content-Type"] == "'application/problem+json'"
      && contains(keys(output.respostas_do_gateway[k].responseTemplates), "application/problem+json")
    ])
    error_message = "THROTTLED e DEFAULT_5XX devem responder com Content-Type application/problem+json."
  }

  # O DEFAULT_4XX fixa 400 para todo 4XX sem resposta própria: os 403 que o cliente vê precisam de entrada explícita.
  assert {
    condition = alltrue([
      for k in ["WAF_FILTERED", "INVALID_API_KEY", "MISSING_AUTHENTICATION_TOKEN"] :
      output.respostas_do_gateway[k].statusCode == 403
      && strcontains(output.respostas_do_gateway[k].responseTemplates["application/problem+json"], "SEM_PERMISSAO")
    ])
    error_message = "WAF_FILTERED, INVALID_API_KEY e MISSING_AUTHENTICATION_TOKEN devem sair 403 SEM_PERMISSAO, não herdar o 400 do DEFAULT_4XX."
  }

  # O gateway valida só parâmetros (Idempotency-Key); o corpo é do serviço (422 do contrato).
  assert {
    condition     = output.validadores.parametros.validateRequestBody == false && output.validadores.parametros.validateRequestParameters == true
    error_message = "O validador do gateway deve validar parâmetros e NÃO o corpo."
  }

  # Junção borda x serviço: toda integração recebe o requestId do gateway.
  assert {
    condition = alltrue(flatten([
      for caminho, metodos in output.parametros_da_integracao : [
        for metodo, p in metodos : p["integration.request.header.X-Gateway-Request-Id"] == "context.requestId"
      ]
    ]))
    error_message = "Toda integração deve receber X-Gateway-Request-Id = context.requestId."
  }

  # WAF: teto por consumidor (x-api-key) e por IP, ambos com 429 + Retry-After e o corpo do catálogo.
  assert {
    condition = alltrue([
      for r in aws_wafv2_web_acl.this.rule :
      r.action[0].block[0].custom_response[0].response_code == 429
      && r.action[0].block[0].custom_response[0].custom_response_body_key == "limite-de-requisicoes"
      && anytrue([for h in r.action[0].block[0].custom_response[0].response_header : h.name == "Retry-After"])
      if contains(["limite-por-consumidor", "limite-por-ip"], r.name)
    ]) && length([for r in aws_wafv2_web_acl.this.rule : r if contains(["limite-por-consumidor", "limite-por-ip"], r.name)]) == 2
    error_message = "As regras rate-based do WAF devem bloquear com 429, Retry-After e o corpo limite-de-requisicoes."
  }

  assert {
    condition = one([
      for r in aws_wafv2_web_acl.this.rule :
      r.statement[0].rate_based_statement[0].custom_key[0].header[0].name == "x-api-key"
      && r.statement[0].rate_based_statement[0].aggregate_key_type == "CUSTOM_KEYS"
      if r.name == "limite-por-consumidor"
    ]) == true
    error_message = "A regra limite-por-consumidor deve agregar pelo header x-api-key."
  }

  assert {
    condition = (
      jsondecode(one(aws_wafv2_web_acl.this.custom_response_body).content).codigo == "LIMITE_DE_REQUISICOES"
      && jsondecode(one(aws_wafv2_web_acl.this.custom_response_body).content).status == 429
    )
    error_message = "O corpo do 429 do WAF deve seguir o catálogo (codigo LIMITE_DE_REQUISICOES, status 429)."
  }

  # D7: toda integração com o timeout do orçamento (10 s), não o teto de 29 s da AWS.
  assert {
    condition = alltrue(flatten([
      for caminho, metodos in output.timeouts_da_integracao : [for metodo, t in metodos : t == 10000]
    ]))
    error_message = "Toda integração deve usar timeoutInMillis = 10000 (orçamento em var.timeout_integracao_ms)."
  }

  # D11: num apply limpo, nenhum plano passa da metade do teto do WAF por consumidor (o plano-carga sobe só via tfvars).
  assert {
    condition = alltrue([
      for p in aws_api_gateway_usage_plan.this :
      p.throttle_settings[0].rate_limit * 300 * 2 <= var.waf_limite_por_consumidor
    ]) && length(aws_api_gateway_usage_plan.this) == 3
    error_message = "Os 3 planos padrão devem ficar a no máximo metade do teto do WAF por consumidor (plano-carga estacionado em 200 rps)."
  }

  # G7-5 (painel 7): 401 do gateway com o desafio que a RFC 9110 §15.5.2 exige, como o 401 do serviço.
  assert {
    condition     = output.respostas_do_gateway["UNAUTHORIZED"].responseParameters["gatewayresponse.header.WWW-Authenticate"] == "'Bearer'"
    error_message = "UNAUTHORIZED precisa levar WWW-Authenticate: Bearer."
  }
}

# --- Observabilidade: todo alarme com ação (tópico SNS com CMK), defasagem do legado e reservas no item ------------
run "observabilidade_alarmes_com_acao" {
  command = plan

  module {
    source = "../../modules/observabilidade"
  }

  variables {
    account_id              = "123456789012"
    api_nome                = "desafio-autorizacao"
    tabelas                 = ["limites"]
    tabelas_com_latencia    = ["limites"]
    cluster_nome            = "desafio"
    servicos_ecs            = ["autorizacao", "limites"]
    filas                   = ["contabilizacao", "antifraude", "notificacoes", "legado-contratos"]
    fila_legado             = "legado-contratos"
    dlqs                    = ["contabilizacao-dlq", "financas-entrega-dlq"]
    relay_funcao            = "desafio-relay"
    cdc_funcao              = "desafio-relay-cdc-limites"
    bus_nome                = "financas"
    regras_do_bus           = ["para-antifraude", "para-contabilizacao", "para-notificacoes", "limite-alterado-para-notificacoes", "para-plataforma-de-dados"]
    funcoes_consumidoras    = ["desafio-contabilizacao", "desafio-consumidor-antifraude", "desafio-consumidor-notificacoes"]
    firehose_nome           = "financas-plataforma-de-dados"
    log_group_limites       = "/ecs/limites"
    log_group_reconciliador = "/ecs/reconciliador"
    log_group_autorizacao   = "/ecs/autorizacao"
    log_group_cdc           = "/aws/lambda/desafio-relay-cdc-limites"
    funcoes_lambda = {
      relay          = "desafio-relay"
      sweeper        = "desafio-sweeper"
      cdc-limites    = "desafio-relay-cdc-limites"
      contabilizacao = "desafio-contabilizacao"
    }
  }

  override_resource {
    target          = aws_sns_topic.alarmes
    override_during = plan
    values = {
      arn = "arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes"
    }
  }

  override_resource {
    target          = aws_kms_key.alarmes
    override_during = plan
    values = {
      arn = "arn:aws:kms:sa-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  }

  # As janelas do burn rate (api_5xx_janela) ficam de fora de propósito: quem notifica é o composto de cada par.
  assert {
    condition = alltrue([
      for a in concat(
        [
          aws_cloudwatch_metric_alarm.api_p99, aws_cloudwatch_metric_alarm.relay_iterator_age, aws_cloudwatch_metric_alarm.cdc_iterator_age,
          aws_cloudwatch_metric_alarm.guarda, aws_cloudwatch_metric_alarm.reservas, aws_cloudwatch_metric_alarm.defasagem_legado,
        ],
        values(aws_cloudwatch_metric_alarm.dlq), values(aws_cloudwatch_metric_alarm.reconciliador),
        values(aws_cloudwatch_metric_alarm.idade_da_fila), values(aws_cloudwatch_metric_alarm.lambda_erros),
        values(aws_cloudwatch_metric_alarm.sinal), values(aws_cloudwatch_composite_alarm.api_5xx_burn),
      ) :
      contains(a.alarm_actions, "arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes")
      && contains(a.ok_actions, "arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes")
    ])
    error_message = "Todo alarme que notifica (inclusive os compostos de burn rate) deve ir ao tópico SNS de alarmes (ALARM e OK)."
  }

  # D3: burn rate de duas janelas sobre o SLO de 99,9% (orçamento 0,1%): 14,4x = 1,44% (1 h E 5 min) e 6x = 0,6% (6 h E 30 min).
  assert {
    condition = (
      abs(aws_cloudwatch_metric_alarm.api_5xx_janela["rapido-1h"].threshold - 1.44) < 0.0001
      && abs(aws_cloudwatch_metric_alarm.api_5xx_janela["rapido-5min"].threshold - 1.44) < 0.0001
      && abs(aws_cloudwatch_metric_alarm.api_5xx_janela["lento-6h"].threshold - 0.6) < 0.0001
      && abs(aws_cloudwatch_metric_alarm.api_5xx_janela["lento-30min"].threshold - 0.6) < 0.0001
      && one([for q in aws_cloudwatch_metric_alarm.api_5xx_janela["rapido-1h"].metric_query : q.metric[0].period if q.id == "erros"]) == 3600
      && one([for q in aws_cloudwatch_metric_alarm.api_5xx_janela["lento-30min"].metric_query : q.metric[0].period if q.id == "total"]) == 1800
    )
    error_message = "Burn rate: janelas 1 h/5 min a 1,44% e 6 h/30 min a 0,6% (14,4x e 6x o orçamento de 0,1%)."
  }

  assert {
    condition = alltrue([
      for k, a in aws_cloudwatch_composite_alarm.api_5xx_burn :
      strcontains(a.alarm_rule, "api-5xx-burn-${k == "rapido" ? "rapido-1h" : "lento-6h"}")
      && strcontains(a.alarm_rule, "api-5xx-burn-${k == "rapido" ? "rapido-5min" : "lento-30min"}")
      && strcontains(a.alarm_rule, " AND ")
    ]) && length(aws_cloudwatch_composite_alarm.api_5xx_burn) == 2
    error_message = "Cada par de burn rate é um alarme composto: janela longa E janela curta."
  }

  # O numerador é o 5XXError do stage, que inclui o 503 do shedding (consome o orçamento por desenho).
  assert {
    condition = alltrue([
      for a in aws_cloudwatch_metric_alarm.api_5xx_janela :
      one([for q in a.metric_query : q.metric[0].metric_name if q.id == "erros"]) == "5XXError"
    ])
    error_message = "O burn rate conta todo 5XX do stage (inclusive o 503 do load shedding)."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.api_p99.threshold == 400 && aws_cloudwatch_metric_alarm.api_p99.threshold > var.p99_limite_ms
    error_message = "O alarme de p99 dispara em 400 ms (acima do SLO de 150 ms, que o entregue não atinge), não no próprio SLO."
  }

  # D2: idade da mensagem mais antiga em TODA fila consumidora; a do legado tem o alarme próprio do ADR-0014.
  assert {
    condition = (
      toset(keys(aws_cloudwatch_metric_alarm.idade_da_fila)) == toset(["contabilizacao", "antifraude", "notificacoes"])
      && alltrue([for a in aws_cloudwatch_metric_alarm.idade_da_fila : a.metric_name == "ApproximateAgeOfOldestMessage" && a.threshold == 300])
    )
    error_message = "Toda fila consumidora (menos a do legado) deve ter alarme de ApproximateAgeOfOldestMessage > 300 s."
  }

  # F-2: Errors >= 1 em cada Lambda; F-6: IteratorAge também no CDC de limites.
  assert {
    condition = (
      toset(keys(aws_cloudwatch_metric_alarm.lambda_erros)) == toset(["relay", "sweeper", "cdc-limites", "contabilizacao"])
      && alltrue([for a in aws_cloudwatch_metric_alarm.lambda_erros : a.metric_name == "Errors" && a.threshold == 1 && a.comparison_operator == "GreaterThanOrEqualToThreshold"])
      && aws_cloudwatch_metric_alarm.cdc_iterator_age.dimensions.FunctionName == "desafio-relay-cdc-limites"
    )
    error_message = "Cada Lambda deve ter alarme de Errors >= 1, e o relay-cdc-limites alarme de IteratorAge."
  }

  # E-N6, F-6 e F7-5/B-1 (painel 7): sinais de log com metric filter no log group certo, no formato da linha de cada um
  # (JSON puro no /ecs/autorizacao; termo onde a linha tem prefixo), cada par de filtros na MESMA métrica do alarme. O
  # termo é o que o código emite (relay/tests/unit/test_sinais_de_log.py cruza os dois lados).
  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.sinal["motivo-desconhecido-autorizacao"].log_group_name == "/ecs/autorizacao"
      && aws_cloudwatch_log_metric_filter.sinal["motivo-desconhecido-autorizacao"].pattern == "{ $.evento = \"motivo_desconhecido_do_limite\" }"
      && aws_cloudwatch_log_metric_filter.sinal["motivo-desconhecido-reconciliador"].log_group_name == "/ecs/reconciliador"
      && aws_cloudwatch_log_metric_filter.sinal["motivo-desconhecido-reconciliador"].pattern == "\"motivo_desconhecido_do_limite\""
      && alltrue([
        for k in ["motivo-desconhecido-autorizacao", "motivo-desconhecido-reconciliador"] :
        aws_cloudwatch_log_metric_filter.sinal[k].metric_transformation[0].name == aws_cloudwatch_metric_alarm.sinal["motivo-desconhecido-do-limite"].metric_name
      ])
      && aws_cloudwatch_log_metric_filter.sinal["cdc-registro-malformado"].log_group_name == "/aws/lambda/desafio-relay-cdc-limites"
      && aws_cloudwatch_log_metric_filter.sinal["cdc-registro-malformado"].pattern == "\"cdc_registro_malformado\""
      && aws_cloudwatch_log_metric_filter.sinal["outbox-registro-malformado-relay"].log_group_name == "/aws/lambda/desafio-relay"
      && aws_cloudwatch_log_metric_filter.sinal["outbox-registro-malformado-sweeper"].log_group_name == "/aws/lambda/desafio-sweeper"
      && alltrue([
        for k in ["outbox-registro-malformado-relay", "outbox-registro-malformado-sweeper"] :
        aws_cloudwatch_log_metric_filter.sinal[k].pattern == "\"outbox_registro_malformado\""
        && aws_cloudwatch_log_metric_filter.sinal[k].metric_transformation[0].name == aws_cloudwatch_metric_alarm.sinal["outbox-registro-malformado"].metric_name
      ])
      && aws_cloudwatch_log_metric_filter.sinal["efeito-sem-evento-autorizacao"].log_group_name == "/ecs/autorizacao"
      && aws_cloudwatch_log_metric_filter.sinal["efeito-sem-evento-autorizacao"].pattern == "{ $.evento = \"efeito_aplicado_com_original_encerrada\" }"
      && aws_cloudwatch_log_metric_filter.sinal["efeito-sem-evento-reconciliador"].log_group_name == "/ecs/reconciliador"
      && alltrue([
        for k in ["efeito-sem-evento-autorizacao", "efeito-sem-evento-reconciliador"] :
        aws_cloudwatch_log_metric_filter.sinal[k].metric_transformation[0].name == aws_cloudwatch_metric_alarm.sinal["efeito-sem-evento"].metric_name
      ])
    )
    error_message = "Metric filters de motivo_desconhecido_do_limite (/ecs/autorizacao JSON, /ecs/reconciliador termo), cdc_registro_malformado (log group do CDC) e outbox_registro_malformado (relay e sweeper, mesma métrica)."
  }

  # Reconciliador: rodada com varredura que levantou (campo varreduras_com_erro) alarma em 2 de ~4 rodadas.
  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.reconciliador["varreduras_com_erro"].pattern == "\"varreduras_com_erro\""
      && aws_cloudwatch_metric_alarm.reconciliador["varreduras_com_erro"].threshold == 2
      && aws_cloudwatch_metric_alarm.reconciliador["varreduras_com_erro"].period == 120
    )
    error_message = "Rodadas com varreduras_com_erro: metric filter por termo e alarme >= 2 em 2 min."
  }

  assert {
    condition     = aws_sns_topic.alarmes.kms_master_key_id == "arn:aws:kms:sa-east-1:123456789012:key/00000000-0000-0000-0000-000000000000" && aws_kms_key.alarmes.enable_key_rotation == true
    error_message = "O tópico de alarmes deve usar a CMK própria (alias/aws/sns não deixa o CloudWatch publicar), com rotação."
  }

  assert {
    condition = (
      jsondecode(aws_sns_topic_policy.alarmes.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
      && jsondecode(aws_sns_topic_policy.alarmes.policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:cloudwatch:sa-east-1:123456789012:alarm:*"
    )
    error_message = "A policy do tópico deve limitar o publish a alarmes desta conta (aws:SourceAccount e aws:SourceArn)."
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 0
    error_message = "Sem e-mail configurado, não há assinatura."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.defasagem_legado.metric_name == "ApproximateAgeOfOldestMessage"
      && aws_cloudwatch_metric_alarm.defasagem_legado.dimensions.QueueName == "legado-contratos"
      && aws_cloudwatch_metric_alarm.defasagem_legado.threshold == 300
    )
    error_message = "Alarme de defasagem do legado: idade da mensagem mais antiga de legado-contratos > 300 s."
  }

  assert {
    condition     = aws_cloudwatch_log_metric_filter.reservas.metric_transformation[0].value == "$.reservas_ativas" && aws_cloudwatch_metric_alarm.reservas.threshold == 2000
    error_message = "Reservas ativas por contrato: metric filter sobre $.reservas_ativas e alarme acima de 2.000."
  }

  assert {
    condition     = contains(keys(aws_cloudwatch_metric_alarm.dlq), "financas-entrega-dlq")
    error_message = "A DLQ de entrega do EventBridge deve ter alarme."
  }

  # Fluxo de eventos ponta a ponta no dashboard (demo). Cada widget é achado pelo título e comparado série a série:
  # as 5 regras do bus (Invocations e FailedInvocations), as filas consumidoras (visíveis e idade da mais antiga, que
  # já cobre antifraude e notificacoes), as funções consumidoras, o Firehose da data platform e as tasks por service.
  assert {
    condition = (
      one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "EventBridge: invocações por regra do bus financas"]) == [
        for r in ["para-antifraude", "para-contabilizacao", "para-notificacoes", "limite-alterado-para-notificacoes", "para-plataforma-de-dados"] :
        ["AWS/Events", "Invocations", "EventBusName", "financas", "RuleName", r]
      ]
      && one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "EventBridge: FailedInvocations por regra (deve ser 0)"]) == [
        for r in ["para-antifraude", "para-contabilizacao", "para-notificacoes", "limite-alterado-para-notificacoes", "para-plataforma-de-dados"] :
        ["AWS/Events", "FailedInvocations", "EventBusName", "financas", "RuleName", r]
      ]
    )
    error_message = "O dashboard deve ter Invocations e FailedInvocations de cada regra do bus financas (3 assinaturas, LimiteAlterado e data platform)."
  }

  assert {
    condition = (
      one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "SQS: mensagens visíveis nas filas consumidoras"]) == [
        for q in ["contabilizacao", "antifraude", "notificacoes", "legado-contratos"] : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q]
      ]
      && one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "SQS: idade da mensagem mais antiga (s)"]) == [
        for q in ["contabilizacao", "antifraude", "notificacoes", "legado-contratos"] : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", q]
      ]
    )
    error_message = "Cada fila consumidora (contabilizacao, antifraude, notificacoes e a do legado) deve ter mensagens visíveis e idade da mais antiga no dashboard."
  }

  assert {
    condition = [
      for s in one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "Consumidores (Lambda): invocações e erros"]) : slice(s, 0, 4)
      ] == [
      for p in setproduct(["Invocations", "Errors"], ["desafio-contabilizacao", "desafio-consumidor-antifraude", "desafio-consumidor-notificacoes"]) :
      ["AWS/Lambda", p[0], "FunctionName", p[1]]
    ]
    error_message = "O dashboard deve ter Invocations e Errors da contabilização e dos 2 consumidores de referência."
  }

  assert {
    condition = (
      [
        for s in one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "Data platform (Firehose): registros entregues no S3 e defasagem (s)"]) :
        [s[0], s[1], s[2], s[3], s[4].stat]
        ] == [
        ["AWS/Firehose", "DeliveryToS3.Records", "DeliveryStreamName", "financas-plataforma-de-dados", "Sum"],
        ["AWS/Firehose", "DeliveryToS3.DataFreshness", "DeliveryStreamName", "financas-plataforma-de-dados", "Maximum"],
      ]
      && one([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.properties.metrics if w.properties.title == "ECS: tasks em execução"]) == [
        for s in ["autorizacao", "limites"] : ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", "desafio", "ServiceName", s]
      ]
    )
    error_message = "O dashboard deve ter DeliveryToS3.Records (soma) e DeliveryToS3.DataFreshness (máximo) do Firehose da data platform e RunningTaskCount (Container Insights) de cada service."
  }

  # Widgets do mesmo tamanho (12 x 6): posição repetida seria um widget desenhado em cima de outro.
  assert {
    condition = (
      length(distinct([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : "${w.x},${w.y}"])) == length(jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets)
      && alltrue([for w in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : w.width == 12 && w.height == 6])
    )
    error_message = "Nenhum widget do dashboard pode ocupar a posição de outro."
  }
}

# --- Pico previsível: janela agendada sobe o mínimo e devolve ao fim (fuso de Brasília) ---------------------------
run "ecs_servico_com_janela_de_pico" {
  command = plan

  module {
    source = "../../modules/ecs_servico"
  }

  variables {
    nome                     = "autorizacao"
    cluster_arn              = "arn:aws:ecs:sa-east-1:123456789012:cluster/desafio"
    cluster_nome             = "desafio"
    namespace_arn            = "arn:aws:servicediscovery:sa-east-1:123456789012:namespace/ns-00000000"
    imagem                   = "123456789012.dkr.ecr.sa-east-1.amazonaws.com/desafio-zup/autorizacao:1.0.0"
    repositorio_ecr_arn      = "arn:aws:ecr:sa-east-1:123456789012:repository/desafio-zup/autorizacao"
    task_role_arn            = "arn:aws:iam::123456789012:role/desafio-task-autorizacao"
    subnet_ids               = ["subnet-00000001"]
    security_group_ids       = ["sg-00000001"]
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    min_tasks                = 2
    max_tasks                = 20
    janelas_de_pico = [{
      nome      = "inicio-de-mes"
      inicio    = "cron(30 6 1 * ? *)"
      fim       = "cron(0 12 1 * ? *)"
      min_tasks = 4
    }]
  }

  assert {
    condition = (
      aws_appautoscaling_scheduled_action.inicio_do_pico["inicio-de-mes"].scalable_target_action[0].min_capacity == "4"
      && aws_appautoscaling_scheduled_action.inicio_do_pico["inicio-de-mes"].timezone == "America/Sao_Paulo"
      && aws_appautoscaling_scheduled_action.fim_do_pico["inicio-de-mes"].scalable_target_action[0].min_capacity == "2"
      && aws_appautoscaling_scheduled_action.fim_do_pico["inicio-de-mes"].scalable_target_action[0].max_capacity == "20"
    )
    error_message = "A janela de pico deve subir o mínimo para 4 no início e voltar a 2..20 no fim, no fuso de Brasília."
  }
}

# --- Scheduler: trust policy com a condição anti-confused-deputy documentada (schedule group) ----------------------
run "lambdas_scheduler_com_source_arn" {
  command = plan

  module {
    source = "../../modules/lambdas"
  }

  variables {
    account_id               = "123456789012"
    zip_relay                = "versions.tf"
    zip_contabilizacao       = "versions.tf"
    bus_nome                 = "financas"
    bus_arn                  = "arn:aws:events:sa-east-1:123456789012:event-bus/financas"
    outbox_tabela_nome       = "autorizacoes-outbox"
    outbox_tabela_arn        = "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox"
    outbox_stream_arn        = "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox/stream/2026-01-01T00:00:00.000"
    limites_stream_arn       = "arn:aws:dynamodb:sa-east-1:123456789012:table/limites/stream/2026-01-01T00:00:00.000"
    fila_contabilizacao_arn  = "arn:aws:sqs:sa-east-1:123456789012:contabilizacao"
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    contabilizacao_tabelas = {
      lancamentos_nome = "contabilizacao-lancamentos"
      lancamentos_arn  = "arn:aws:dynamodb:sa-east-1:123456789012:table/contabilizacao-lancamentos"
      inbox_nome       = "contabilizacao-inbox"
      inbox_arn        = "arn:aws:dynamodb:sa-east-1:123456789012:table/contabilizacao-inbox"
    }
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
      && jsondecode(aws_iam_role.scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == "arn:aws:scheduler:sa-east-1:123456789012:schedule-group/default"
      && aws_scheduler_schedule.sweeper.group_name == "default"
    )
    error_message = "A role do scheduler só pode ser assumida a partir do schedule group default desta conta."
  }

  # B-7: o relay só é invocado por INSERT (o REMOVE que ele mesmo e o TTL geram não chega à função).
  assert {
    condition     = jsondecode(one(one(aws_lambda_event_source_mapping.relay.filter_criteria).filter).pattern).eventName == ["INSERT"]
    error_message = "O ESM do relay deve filtrar eventName = INSERT."
  }

  # D2: teto do ESM da contabilização dimensionado para o pico (~10.000 eventos/s no pior mix), não os 20 de antes.
  assert {
    condition     = aws_lambda_event_source_mapping.contabilizacao.scaling_config[0].maximum_concurrency == 200
    error_message = "O ESM da contabilização deve ter maximum_concurrency = 200 (conta em modules/lambdas/variables.tf)."
  }
}

# --- Rede leste-oeste: SG por serviço; a 8080 do limites só aceita os SGs do autorizacao e do reconciliador ---------
run "rede_leste_oeste" {
  command = plan

  module {
    source = "../../modules/rede"
  }

  variables {
    nome = "desafio"
  }

  override_resource {
    target          = aws_security_group.autorizacao
    override_during = plan
    values = {
      id = "sg-autorizacao"
    }
  }

  override_resource {
    target          = aws_security_group.limites
    override_during = plan
    values = {
      id = "sg-limites"
    }
  }

  override_resource {
    target          = aws_security_group.limites_acl
    override_during = plan
    values = {
      id = "sg-limites-acl"
    }
  }

  override_resource {
    target          = aws_security_group.reconciliador
    override_during = plan
    values = {
      id = "sg-reconciliador"
    }
  }

  assert {
    condition = (
      toset([for r in aws_vpc_security_group_ingress_rule.limites_dos_clientes : r.referenced_security_group_id]) == toset(["sg-autorizacao", "sg-reconciliador"])
      && alltrue([
        for r in aws_vpc_security_group_ingress_rule.limites_dos_clientes :
        r.security_group_id == "sg-limites" && r.cidr_ipv4 == null && r.from_port == 8080 && r.to_port == 8080
      ])
    )
    error_message = "A 8080 do limites só aceita os SGs do autorizacao e do reconciliador, por referência (sem CIDR)."
  }

  # O NLB não tem SG (não dá para associar depois de criado): o autorizacao aceita a 8080 do CIDR da VPC, e só ele.
  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.autorizacao_do_nlb.security_group_id == "sg-autorizacao"
      && aws_vpc_security_group_ingress_rule.autorizacao_do_nlb.cidr_ipv4 == "10.40.0.0/16"
      && aws_vpc_security_group_ingress_rule.autorizacao_do_nlb.from_port == 8080
      && aws_vpc_security_group_ingress_rule.autorizacao_do_nlb.to_port == 8080
    )
    error_message = "O autorizacao aceita a 8080 do CIDR da VPC (NLB sem SG)."
  }

  # Workers sem entrada: as únicas regras de ingress do módulo são do autorizacao e do limites.
  assert {
    condition = toset(concat(
      [aws_vpc_security_group_ingress_rule.autorizacao_do_nlb.security_group_id],
      [for r in aws_vpc_security_group_ingress_rule.limites_dos_clientes : r.security_group_id],
    )) == toset(["sg-autorizacao", "sg-limites"])
    error_message = "limites-acl e reconciliador não podem ter regra de entrada."
  }

  assert {
    condition = (
      toset([for r in aws_vpc_security_group_egress_rule.clientes_para_limites : r.security_group_id]) == toset(["sg-autorizacao", "sg-reconciliador"])
      && alltrue([for r in aws_vpc_security_group_egress_rule.clientes_para_limites : r.referenced_security_group_id == "sg-limites" && r.cidr_ipv4 == null])
    )
    error_message = "A saída 8080 existe só no autorizacao e no reconciliador, e só para o SG do limites."
  }

  assert {
    condition = (
      toset([for r in aws_vpc_security_group_egress_rule.https : r.security_group_id]) == toset(["sg-autorizacao", "sg-limites", "sg-limites-acl", "sg-reconciliador"])
      && alltrue([for r in aws_vpc_security_group_egress_rule.https : r.from_port == 443 && r.to_port == 443])
    )
    error_message = "Todo SG de serviço sai só na 443 para as APIs AWS."
  }
}

# --- Raiz: policies das task roles sem Action "*" / "dynamodb:*" e sem Resource "*" ------------------
run "task_roles_menor_privilegio" {
  command = plan

  variables {
    account_id         = "123456789012"
    zip_relay          = "versions.tf" # os zips reais vêm de `make pacotes`; no plan basta o arquivo existir
    zip_contabilizacao = "versions.tf"
  }

  override_module {
    target = module.eventos
    outputs = {
      bus_nome             = "financas"
      bus_arn              = "arn:aws:events:sa-east-1:123456789012:event-bus/financas"
      filas_arns           = { contabilizacao = "arn:aws:sqs:sa-east-1:123456789012:contabilizacao" }
      filas_nomes          = { contabilizacao = "contabilizacao" }
      dlqs_nomes           = { contabilizacao = "contabilizacao-dlq" }
      fila_legado_arn      = "arn:aws:sqs:sa-east-1:123456789012:legado-contratos"
      fila_legado_nome     = "legado-contratos"
      fila_legado_dlq_nome = "legado-contratos-dlq"
      entrega_dlq_nome     = "financas-entrega-dlq"
      regras_nomes         = ["para-contabilizacao"]
    }
  }

  override_module {
    target = module.dados
    outputs = {
      nomes = {
        limites                      = "limites"
        autorizacoes                 = "autorizacoes"
        "autorizacoes-outbox"        = "autorizacoes-outbox"
        "contabilizacao-lancamentos" = "contabilizacao-lancamentos"
        "contabilizacao-inbox"       = "contabilizacao-inbox"
      }
      arns = {
        limites                      = "arn:aws:dynamodb:sa-east-1:123456789012:table/limites"
        autorizacoes                 = "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes"
        "autorizacoes-outbox"        = "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox"
        "contabilizacao-lancamentos" = "arn:aws:dynamodb:sa-east-1:123456789012:table/contabilizacao-lancamentos"
        "contabilizacao-inbox"       = "arn:aws:dynamodb:sa-east-1:123456789012:table/contabilizacao-inbox"
      }
      stream_arns = {
        limites               = "arn:aws:dynamodb:sa-east-1:123456789012:table/limites/stream/2026-01-01T00:00:00.000"
        "autorizacoes-outbox" = "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox/stream/2026-01-01T00:00:00.000"
      }
    }
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.task_autorizacao.policy, aws_iam_role_policy.task_limites.policy] :
      alltrue([
        for s in jsondecode(p).Statement :
        !anytrue([for a in tolist(s.Action) : a == "*" || a == "dynamodb:*"])
      ])
    ])
    error_message = "Policies das task roles não podem ter Action \"*\" nem \"dynamodb:*\"."
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.task_autorizacao.policy, aws_iam_role_policy.task_limites.policy] :
      alltrue([
        for s in jsondecode(p).Statement :
        alltrue([for r in tolist(s.Resource) : startswith(r, "arn:aws:dynamodb:")])
      ])
    ])
    error_message = "Policies das task roles devem restringir Resource a ARNs de tabelas DynamoDB."
  }

  # Task roles novas (ACL do legado e reconciliador): sem Action coringa e com o recurso certo.
  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.task_limites_acl.policy, aws_iam_role_policy.task_reconciliador.policy] :
      alltrue([
        for s in jsondecode(p).Statement :
        !anytrue([for a in tolist(s.Action) : a == "*" || a == "dynamodb:*" || a == "sqs:*"])
      ])
    ])
    error_message = "Policies de limites-acl e reconciliador não podem ter Action coringa."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.task_reconciliador.policy).Statement :
      alltrue([for r in tolist(s.Resource) : startswith(r, "arn:aws:dynamodb:")])
    ])
    error_message = "A policy do reconciliador deve restringir Resource a ARNs de tabelas/índices DynamoDB."
  }

  assert {
    condition = contains(
      flatten([for s in jsondecode(aws_iam_role_policy.task_reconciliador.policy).Statement : tolist(s.Resource)]),
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes/index/gsi_orfas"
    )
    error_message = "O reconciliador deve consultar o índice gsi_orfas."
  }

  assert {
    condition = contains(
      flatten([for s in jsondecode(aws_iam_role_policy.task_reconciliador.policy).Statement : tolist(s.Resource)]),
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes/index/gsi_vencimentos"
    )
    error_message = "O reconciliador deve consultar o índice gsi_vencimentos."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.task_limites_acl.policy).Statement :
      alltrue([for r in tolist(s.Resource) : r == "arn:aws:sqs:sa-east-1:123456789012:legado-contratos" || r == "arn:aws:dynamodb:sa-east-1:123456789012:table/limites"])
    ])
    error_message = "A ACL só pode tocar a fila do legado e a tabela limites."
  }

  # Database-per-service (MS-03): allowlist EXCLUSIVA de recursos por task role. Um GetItem do autorizacao na tabela
  # limites passaria nos asserts de forma acima; aqui não passa.
  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.task_autorizacao.policy).Statement : tolist(s.Resource)])) == toset([
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes",
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox",
    ])
    error_message = "O autorizacao só pode tocar as tabelas autorizacoes e autorizacoes-outbox."
  }

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.task_limites.policy).Statement : tolist(s.Resource)])) == toset([
      "arn:aws:dynamodb:sa-east-1:123456789012:table/limites",
    ])
    error_message = "O limites só pode tocar a tabela limites."
  }

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.task_reconciliador.policy).Statement : tolist(s.Resource)])) == toset([
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes",
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes/index/gsi_orfas",
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes/index/gsi_vencimentos",
      "arn:aws:dynamodb:sa-east-1:123456789012:table/autorizacoes-outbox",
    ])
    error_message = "O reconciliador só pode tocar autorizacoes (tabela e os 2 índices) e autorizacoes-outbox."
  }

  assert {
    condition = alltrue([
      for r in [aws_iam_role.task_autorizacao, aws_iam_role.task_limites, aws_iam_role.task_limites_acl, aws_iam_role.task_reconciliador] :
      r.permissions_boundary == "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    ])
    error_message = "Toda task role carrega o permissions boundary (a esteira só cria role com ele)."
  }

  # Anti-confused-deputy documentado pelo ECS para a task role: aws:SourceAccount + aws:SourceArn (curinga de cluster).
  assert {
    condition = alltrue([
      for r in [aws_iam_role.task_autorizacao, aws_iam_role.task_limites, aws_iam_role.task_limites_acl, aws_iam_role.task_reconciliador] :
      jsondecode(r.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
      && jsondecode(r.assume_role_policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:ecs:sa-east-1:123456789012:*"
    ])
    error_message = "As task roles devem exigir aws:SourceAccount e aws:SourceArn (ECS desta conta e região)."
  }
}

# --- Schema registry: os 8 contratos de contracts/events, com o conteúdo do arquivo e o tipo draft-04 -------------
run "eventos_schema_registry_com_os_contratos" {
  command = plan

  module {
    source = "../../modules/eventos"
  }

  variables {
    regras_externas_na_dlq_de_entrega = ["arn:aws:events:sa-east-1:123456789012:rule/financas/para-plataforma-de-dados"]
  }

  override_resource {
    target          = aws_sqs_queue.entrega_dlq
    override_during = plan
    values = {
      arn = "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
    }
  }

  override_resource {
    target          = aws_cloudwatch_event_rule.this
    override_during = plan
    values = {
      arn = "arn:aws:events:sa-east-1:123456789012:rule/financas/para-x"
    }
  }

  override_resource {
    target          = aws_cloudwatch_event_rule.limite_alterado
    override_during = plan
    values = {
      arn = "arn:aws:events:sa-east-1:123456789012:rule/financas/limite-alterado-para-notificacoes"
    }
  }

  assert {
    condition     = aws_schemas_registry.this.name == "financas"
    error_message = "O registry deve ter o nome do bus (financas)."
  }

  assert {
    condition = toset(keys(aws_schemas_schema.this)) == toset([
      "limite-alterado.v1", "reserva-capturada.v1", "reserva-expirada.v1", "transacao-autorizada.v1",
      "transacao-compensada.v1", "transacao-em-reconciliacao-manual.v1", "transacao-estornada.v1", "transacao-recusada.v1",
    ])
    error_message = "O registry deve publicar exatamente os 8 contratos de contracts/events/*.schema.json."
  }

  assert {
    condition = alltrue([
      for k, s in aws_schemas_schema.this :
      s.content == file("../../../../contracts/events/${k}.schema.json")
      && s.type == "JSONSchemaDraft4"
      && s.registry_name == "financas"
    ])
    error_message = "Cada schema deve ter o conteúdo exato do arquivo do repositório, tipo JSONSchemaDraft4 e ficar no registry financas."
  }

  assert {
    condition = (
      aws_schemas_schema.this["transacao-autorizada.v1"].name == "example.financas.autorizacao.TransacaoAutorizada.v1"
      && aws_schemas_schema.this["limite-alterado.v1"].name == "example.financas.limites.LimiteAlterado.v1"
    )
    error_message = "O nome do schema deve ser o atributo `type` do CloudEvent do contrato."
  }

  # A regra da data platform (criada fora do módulo) também grava na DLQ de entrega.
  assert {
    condition = (
      contains(
        jsondecode(aws_sqs_queue_policy.entrega_dlq.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"],
        "arn:aws:events:sa-east-1:123456789012:rule/financas/para-plataforma-de-dados"
      )
      && length(jsondecode(aws_sqs_queue_policy.entrega_dlq.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == 5
    )
    error_message = "A policy da DLQ de entrega deve aceitar as 4 regras do módulo e a regra externa da data platform."
  }

  # Nomes que a raiz passa ao dashboard (dimensão RuleName): os mesmos que o CloudWatch publica no bus financas.
  assert {
    condition     = output.regras_nomes == ["para-antifraude", "para-contabilizacao", "para-notificacoes", "limite-alterado-para-notificacoes"]
    error_message = "regras_nomes deve trazer as 3 regras de assinatura e a do LimiteAlterado."
  }
}

# --- Data platform: regra do bus -> Firehose (Direct PUT) -> S3 endurecido; roles de menor privilégio ----------------
run "plataforma_de_dados_firehose_para_s3" {
  command = plan

  module {
    source = "../../modules/plataforma_de_dados"
  }

  variables {
    account_id               = "123456789012"
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    bus_nome                 = "financas"
    dlq_de_entrega_arn       = "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
    acoes_alarme             = ["arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes"]
  }

  override_resource {
    target          = aws_s3_bucket.this
    override_during = plan
    values = {
      arn = "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012"
    }
  }

  override_resource {
    target          = aws_cloudwatch_log_group.firehose
    override_during = plan
    values = {
      arn = "arn:aws:logs:sa-east-1:123456789012:log-group:/aws/kinesisfirehose/financas-plataforma-de-dados"
    }
  }

  override_resource {
    target          = aws_kinesis_firehose_delivery_stream.this
    override_during = plan
    values = {
      arn = "arn:aws:firehose:sa-east-1:123456789012:deliverystream/financas-plataforma-de-dados"
    }
  }

  override_resource {
    target          = aws_cloudwatch_event_rule.this
    override_during = plan
    values = {
      arn = "arn:aws:events:sa-east-1:123456789012:rule/financas/para-plataforma-de-dados"
    }
  }

  override_resource {
    target          = aws_iam_role.eventbridge
    override_during = plan
    values = {
      arn = "arn:aws:iam::123456789012:role/desafio-eventbridge-financas-plataforma-de-dados"
    }
  }

  # Bucket: SSE-KMS com bucket key, versionado, Block Public Access, ACLs desligadas, lifecycle de 30 dias, só TLS.
  assert {
    condition = (
      aws_s3_bucket.this.bucket == "desafio-zup-plataforma-de-dados-123456789012"
      && one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms"
      && one(aws_s3_bucket_server_side_encryption_configuration.this.rule).bucket_key_enabled == true
      && aws_s3_bucket_versioning.this.versioning_configuration[0].status == "Enabled"
      && aws_s3_bucket_ownership_controls.this.rule[0].object_ownership == "BucketOwnerEnforced"
    )
    error_message = "O bucket deve ter SSE-KMS com bucket key, versionamento e ACLs desligadas."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.this.block_public_acls,
      aws_s3_bucket_public_access_block.this.block_public_policy,
      aws_s3_bucket_public_access_block.this.ignore_public_acls,
      aws_s3_bucket_public_access_block.this.restrict_public_buckets,
    ])
    error_message = "O bucket deve ter o Block Public Access completo."
  }

  assert {
    condition = (
      aws_s3_bucket_lifecycle_configuration.this.rule[0].status == "Enabled"
      && aws_s3_bucket_lifecycle_configuration.this.rule[0].expiration[0].days == 30
      && aws_s3_bucket_lifecycle_configuration.this.rule[0].noncurrent_version_expiration[0].noncurrent_days == 1
      && aws_s3_bucket_lifecycle_configuration.this.rule[0].abort_incomplete_multipart_upload[0].days_after_initiation == 1
    )
    error_message = "O lifecycle do sandbox deve expirar objetos em 30 dias, versões antigas em 1 e multipart abandonado em 1."
  }

  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.this.policy).Statement[0].Effect == "Deny"
      && jsondecode(aws_s3_bucket_policy.this.policy).Statement[0].Action == "s3:*"
      && jsondecode(aws_s3_bucket_policy.this.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
      && toset(jsondecode(aws_s3_bucket_policy.this.policy).Statement[0].Resource) == toset([
        "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012",
        "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012/*",
      ])
    )
    error_message = "A bucket policy deve negar tudo sem TLS, no bucket e nos objetos."
  }

  # Firehose Direct PUT (sem fonte Kinesis/MSK), buffer de 60 s, partição por tipo e data, NDJSON em GZIP, log de erro.
  assert {
    condition = (
      aws_kinesis_firehose_delivery_stream.this.destination == "extended_s3"
      && length(aws_kinesis_firehose_delivery_stream.this.kinesis_source_configuration) == 0
      && length(aws_kinesis_firehose_delivery_stream.this.msk_source_configuration) == 0
      && aws_kinesis_firehose_delivery_stream.this.server_side_encryption[0].enabled == true
    )
    error_message = "O Firehose deve ser Direct PUT para S3, com o buffer cifrado."
  }

  assert {
    condition = (
      aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].bucket_arn == "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012"
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].buffering_interval == 60
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].buffering_size == 64
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].compression_format == "GZIP"
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].prefix == "eventos/tipo=!{partitionKeyFromQuery:tipo}/ano=!{timestamp:yyyy}/mes=!{timestamp:MM}/dia=!{timestamp:dd}/"
      && startswith(aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].error_output_prefix, "erros/!{firehose:error-output-type}/")
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].dynamic_partitioning_configuration[0].enabled == true
    )
    error_message = "O Firehose deve gravar no bucket com buffer de 60 s, GZIP e prefixo particionado por tipo e data (dynamic partitioning)."
  }

  assert {
    condition = (
      contains([
        for p in aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].processing_configuration[0].processors :
        p.type == "MetadataExtraction" && contains([for x in p.parameters : "${x.parameter_name}=${x.parameter_value}"], "MetadataExtractionQuery={tipo: .[\"detail-type\"]}")
      ], true)
      && contains([for p in aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].processing_configuration[0].processors : p.type], "AppendDelimiterToRecord")
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].cloudwatch_logging_options[0].enabled == true
      && aws_kinesis_firehose_delivery_stream.this.extended_s3_configuration[0].cloudwatch_logging_options[0].log_stream_name == "DestinationDelivery"
    )
    error_message = "A chave `tipo` deve vir do detail-type (JQ), um evento por linha, e as falhas de entrega devem ir para o log."
  }

  # Role do Firehose: trust com sts:ExternalId; só PutObject nos dois prefixos e PutLogEvents no stream de erro.
  assert {
    condition = (
      jsondecode(aws_iam_role.firehose.assume_role_policy).Statement[0].Principal.Service == "firehose.amazonaws.com"
      && jsondecode(aws_iam_role.firehose.assume_role_policy).Statement[0].Condition.StringEquals["sts:ExternalId"] == "123456789012"
      && aws_iam_role.firehose.permissions_boundary == "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    )
    error_message = "A role do Firehose deve exigir sts:ExternalId da conta e carregar o permissions boundary."
  }

  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.firehose.policy).Statement : s.Action])) == toset(["s3:PutObject", "logs:PutLogEvents", "kms:GenerateDataKey", "kms:Decrypt"])
      && toset(flatten([for s in jsondecode(aws_iam_role_policy.firehose.policy).Statement : s.Resource if s.Resource != "*"])) == toset([
        "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012/eventos/*",
        "arn:aws:s3:::desafio-zup-plataforma-de-dados-123456789012/erros/*",
        "arn:aws:logs:sa-east-1:123456789012:log-group:/aws/kinesisfirehose/financas-plataforma-de-dados:log-stream:DestinationDelivery",
      ])
      # A chave aws/s3 só serve via S3 na região: o "*" do KMS vem sempre amarrado a kms:ViaService.
      && length([for s in jsondecode(aws_iam_role_policy.firehose.policy).Statement : s if s.Resource == "*"]) == 1
      && alltrue([
        for s in jsondecode(aws_iam_role_policy.firehose.policy).Statement :
        toset(s.Action) == toset(["kms:GenerateDataKey", "kms:Decrypt"])
        && s.Condition.StringEquals["kms:ViaService"] == "s3.sa-east-1.amazonaws.com"
        if s.Resource == "*"
      ])
    )
    error_message = "A role do Firehose só pode gravar objetos nos prefixos eventos/ e erros/, logar no stream de erro e usar KMS via S3."
  }

  # Assinatura: todos os eventos dos dois sources do bus, entregues com role própria, retry e DLQ de entrega.
  assert {
    condition = (
      aws_cloudwatch_event_rule.this.event_bus_name == "financas"
      && aws_cloudwatch_event_rule.this.state == "ENABLED"
      && jsondecode(aws_cloudwatch_event_rule.this.event_pattern) == { source = ["financas.autorizacao", "financas.limites"] }
    )
    error_message = "A regra deve casar todo evento de financas.autorizacao e financas.limites no bus financas."
  }

  assert {
    condition = (
      aws_cloudwatch_event_target.this.arn == "arn:aws:firehose:sa-east-1:123456789012:deliverystream/financas-plataforma-de-dados"
      && aws_cloudwatch_event_target.this.role_arn == "arn:aws:iam::123456789012:role/desafio-eventbridge-financas-plataforma-de-dados"
      && aws_cloudwatch_event_target.this.dead_letter_config[0].arn == "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
      && aws_cloudwatch_event_target.this.retry_policy[0].maximum_event_age_in_seconds == 3600
      && aws_cloudwatch_event_target.this.retry_policy[0].maximum_retry_attempts == 185
    )
    error_message = "O target deve entregar no Firehose pela role própria, com retry de 1 h/185 tentativas e a DLQ de entrega."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.eventbridge.assume_role_policy).Statement[0].Principal.Service == "events.amazonaws.com"
      && jsondecode(aws_iam_role.eventbridge.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:events:sa-east-1:123456789012:rule/financas/para-plataforma-de-dados"
      && jsondecode(aws_iam_role.eventbridge.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
      && aws_iam_role.eventbridge.permissions_boundary == "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
      && toset(flatten([for s in jsondecode(aws_iam_role_policy.eventbridge.policy).Statement : s.Action])) == toset(["firehose:PutRecord", "firehose:PutRecordBatch"])
      && toset(flatten([for s in jsondecode(aws_iam_role_policy.eventbridge.policy).Statement : s.Resource])) == toset(["arn:aws:firehose:sa-east-1:123456789012:deliverystream/financas-plataforma-de-dados"])
    )
    error_message = "A role do EventBridge deve ser presa à regra e à conta e só gravar neste stream."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.defasagem.namespace == "AWS/Firehose"
      && aws_cloudwatch_metric_alarm.defasagem.metric_name == "DeliveryToS3.DataFreshness"
      && aws_cloudwatch_metric_alarm.defasagem.dimensions["DeliveryStreamName"] == "financas-plataforma-de-dados"
      && aws_cloudwatch_metric_alarm.defasagem.threshold == 900
      && aws_cloudwatch_metric_alarm.defasagem.alarm_actions == toset(["arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes"])
    )
    error_message = "O alarme de defasagem da entrega no S3 deve notificar o tópico de alarmes."
  }

  # Nomes que a raiz passa ao dashboard (RuleName do EventBridge e DeliveryStreamName do Firehose).
  assert {
    condition     = output.regra_nome == "para-plataforma-de-dados" && output.firehose_nome == "financas-plataforma-de-dados"
    error_message = "A regra da data platform é para-plataforma-de-dados e o Firehose é financas-plataforma-de-dados."
  }
}

# Janela de medição de carga: a regra fica desligada (sem invocation nem Firehose), o resto continua de pé.
run "plataforma_de_dados_desligada_na_medicao" {
  command = plan

  module {
    source = "../../modules/plataforma_de_dados"
  }

  variables {
    account_id               = "123456789012"
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    bus_nome                 = "financas"
    dlq_de_entrega_arn       = "arn:aws:sqs:sa-east-1:123456789012:financas-entrega-dlq"
    ativa                    = false
  }

  assert {
    condition     = aws_cloudwatch_event_rule.this.state == "DISABLED"
    error_message = "Com ativa = false a regra da data platform deve ficar DISABLED."
  }
}

# --- Predictive scaling: só previsão (ForecastOnly) junto do target tracking; desligado por padrão --------------------
run "ecs_servico_com_escala_preditiva" {
  command = plan

  module {
    source = "../../modules/ecs_servico"
  }

  variables {
    nome                     = "autorizacao"
    cluster_arn              = "arn:aws:ecs:sa-east-1:123456789012:cluster/desafio"
    cluster_nome             = "desafio"
    namespace_arn            = "arn:aws:servicediscovery:sa-east-1:123456789012:namespace/ns-00000000"
    imagem                   = "123456789012.dkr.ecr.sa-east-1.amazonaws.com/desafio-zup/autorizacao:1.0.0"
    repositorio_ecr_arn      = "arn:aws:ecr:sa-east-1:123456789012:repository/desafio-zup/autorizacao"
    task_role_arn            = "arn:aws:iam::123456789012:role/desafio-task-autorizacao"
    subnet_ids               = ["subnet-00000001"]
    security_group_ids       = ["sg-00000001"]
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
    escala_preditiva         = "ForecastOnly"
  }

  assert {
    condition = (
      length(aws_appautoscaling_policy.previsao) == 1
      && aws_appautoscaling_policy.previsao[0].policy_type == "PredictiveScaling"
      && aws_appautoscaling_policy.previsao[0].service_namespace == "ecs"
      && aws_appautoscaling_policy.previsao[0].scalable_dimension == "ecs:service:DesiredCount"
      && aws_appautoscaling_policy.previsao[0].resource_id == "service/desafio/autorizacao"
      && aws_appautoscaling_policy.previsao[0].predictive_scaling_policy_configuration[0].mode == "ForecastOnly"
    )
    error_message = "O service deve ter política PredictiveScaling em ForecastOnly no mesmo alvo de autoscaling."
  }

  assert {
    condition = (
      aws_appautoscaling_policy.previsao[0].predictive_scaling_policy_configuration[0].metric_specification[0].predefined_metric_pair_specification[0].predefined_metric_type == "ECSServiceCPUUtilization"
      && aws_appautoscaling_policy.previsao[0].predictive_scaling_policy_configuration[0].metric_specification[0].target_value == "60"
      && aws_appautoscaling_policy.cpu.policy_type == "TargetTrackingScaling"
    )
    error_message = "A previsão deve usar o par de métricas de CPU do ECS com o alvo do target tracking (60%), que continua existindo."
  }
}

run "ecs_servico_sem_escala_preditiva_por_padrao" {
  command = plan

  module {
    source = "../../modules/ecs_servico"
  }

  variables {
    nome                     = "reconciliador"
    cluster_arn              = "arn:aws:ecs:sa-east-1:123456789012:cluster/desafio"
    cluster_nome             = "desafio"
    namespace_arn            = "arn:aws:servicediscovery:sa-east-1:123456789012:namespace/ns-00000000"
    imagem                   = "123456789012.dkr.ecr.sa-east-1.amazonaws.com/desafio-zup/autorizacao:1.0.0"
    repositorio_ecr_arn      = "arn:aws:ecr:sa-east-1:123456789012:repository/desafio-zup/autorizacao"
    task_role_arn            = "arn:aws:iam::123456789012:role/desafio-task-reconciliador"
    subnet_ids               = ["subnet-00000001"]
    security_group_ids       = ["sg-00000001"]
    limite_de_permissoes_arn = "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
  }

  assert {
    condition     = length(aws_appautoscaling_policy.previsao) == 0
    error_message = "Sem escala_preditiva o service não tem política preditiva."
  }
}

# --- Raiz: previsão ligada só em autorizacao e limites; registry e bucket da data platform no ambiente ---------------
run "raiz_liga_previsao_registry_e_data_platform" {
  command = plan

  variables {
    account_id         = "123456789012"
    zip_relay          = "versions.tf" # os zips reais vêm de `make pacotes`; no plan basta o arquivo existir
    zip_contabilizacao = "versions.tf"
    zip_consumidores   = "versions.tf"
  }

  assert {
    condition = (
      module.autorizacao.escala_preditiva_modo == "ForecastOnly"
      && module.limites.escala_preditiva_modo == "ForecastOnly"
      && module.limites_acl.escala_preditiva_modo == null
      && module.reconciliador.escala_preditiva_modo == null
    )
    error_message = "A previsão (ForecastOnly) deve estar em autorizacao e limites, e não nos workers de 1 task."
  }

  assert {
    condition = (
      output.schema_registry == "financas"
      && output.bucket_plataforma_de_dados == "desafio-zup-plataforma-de-dados-123456789012"
    )
    error_message = "O ambiente deve publicar o registry financas e o bucket da data platform."
  }
}
