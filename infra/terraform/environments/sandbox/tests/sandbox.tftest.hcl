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
    servicos_ecs            = ["autorizacao"]
    filas                   = ["contabilizacao", "legado-contratos"]
    fila_legado             = "legado-contratos"
    dlqs                    = ["contabilizacao-dlq", "financas-entrega-dlq"]
    relay_funcao            = "desafio-relay"
    log_group_limites       = "/ecs/limites"
    log_group_reconciliador = "/ecs/reconciliador"
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

  assert {
    condition = alltrue([
      for a in concat(
        [
          aws_cloudwatch_metric_alarm.api_5xx, aws_cloudwatch_metric_alarm.api_p99, aws_cloudwatch_metric_alarm.relay_iterator_age,
          aws_cloudwatch_metric_alarm.guarda, aws_cloudwatch_metric_alarm.reservas, aws_cloudwatch_metric_alarm.defasagem_legado,
        ],
        values(aws_cloudwatch_metric_alarm.dlq), values(aws_cloudwatch_metric_alarm.reconciliador),
      ) :
      contains(a.alarm_actions, "arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes")
      && contains(a.ok_actions, "arn:aws:sns:sa-east-1:123456789012:desafio-autorizacao-alarmes")
    ])
    error_message = "Todo alarme deve notificar o tópico SNS de alarmes (ALARM e OK)."
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
