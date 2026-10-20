# Testes do ambiente sandbox com provider mockado: só `plan`, nenhuma chamada à AWS.
# Execução: make tf-test. O diretório tests/ do ambiente é um symlink para este (o terraform test exige
# o diretório de testes DENTRO do diretório da configuração).
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
}
