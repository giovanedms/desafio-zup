# Consumidores de referência das filas antifraude e notificacoes (modules/lambdas + bloco próprio do main.tf).
# Provider mockado, nenhuma chamada à AWS. Execução: make tf-test (roda todos os *.tftest.hcl deste diretório).

# No `apply` mockado o provider confere o formato do ARN da role nas funções; os demais ARNs ficam aleatórios, um por
# instância (é o que o primeiro run usa para distinguir os recursos de cada consumidor).
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }
}

variables {
  fila_antifraude   = "arn:aws:sqs:sa-east-1:123456789012:antifraude"
  fila_notificacoes = "arn:aws:sqs:sa-east-1:123456789012:notificacoes"
}

# --- Módulo: uma função, um ESM, uma role, um log group e uma inbox por fila; cada role só alcança o que é dela --------
# `apply` com o provider mockado (nada sai da máquina): cada instância ganha um ARN próprio, e dá para provar que a
# policy de um consumidor aponta para a inbox e o log group DELE, não para os do outro.
run "consumidores_menor_privilegio_e_filas_certas" {
  command = apply

  module {
    source = "../../modules/lambdas"
  }

  variables {
    account_id               = "123456789012"
    zip_relay                = "versions.tf"
    zip_contabilizacao       = "versions.tf"
    zip_consumidores         = "versions.tf"
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
    consumidores = {
      antifraude   = var.fila_antifraude
      notificacoes = var.fila_notificacoes
    }
  }

  # O schedule do sweeper também confere o formato do ARN do alvo.
  override_resource {
    target = aws_lambda_function.sweeper
    values = {
      arn = "arn:aws:lambda:sa-east-1:123456789012:function:desafio-sweeper"
    }
  }

  assert {
    condition = (
      toset(keys(aws_lambda_function.consumidor)) == toset(["antifraude", "notificacoes"])
      && alltrue([
        for k, f in aws_lambda_function.consumidor :
        f.function_name == "desafio-consumidor-${k}" && f.handler == "consumidores.lambda_sqs.handler"
        && f.runtime == "python3.13" && tolist(f.architectures) == tolist(["arm64"]) && f.tracing_config[0].mode == "Active"
        && f.environment[0].variables.CONSUMIDOR == k
        && f.environment[0].variables.TABELA_INBOX == aws_dynamodb_table.inbox_consumidor[k].name
        && f.role == aws_iam_role.consumidor[k].arn
        && f.logging_config[0].log_group == "/aws/lambda/desafio-consumidor-${k}"
      ])
    )
    error_message = "Uma função por fila (desafio-consumidor-<fila>, py3.13 arm64, X-Ray), com a role, a inbox e o log group dela."
  }

  # Cada ESM lê a fila do SEU consumidor e devolve só o que falhou (ReportBatchItemFailures), com teto de concorrência.
  assert {
    condition = alltrue([
      for k, m in aws_lambda_event_source_mapping.consumidor :
      m.event_source_arn == { antifraude = var.fila_antifraude, notificacoes = var.fila_notificacoes }[k]
      && m.function_name == aws_lambda_function.consumidor[k].arn
      && m.function_response_types == toset(["ReportBatchItemFailures"])
      && m.batch_size == 10
      && m.scaling_config[0].maximum_concurrency == 100
    ]) && length(aws_lambda_event_source_mapping.consumidor) == 2
    error_message = "ESM de cada consumidor: a fila dele, a função dele, ReportBatchItemFailures, lote de 10 e teto de 100."
  }

  # Menor privilégio: a allowlist EXATA de cada statement. Um consumidor que lesse a fila ou a inbox do outro, ou
  # ganhasse GetItem/Scan/DeleteItem, falha aqui.
  assert {
    condition = alltrue([
      for k, p in aws_iam_role_policy.consumidor :
      { for s in jsondecode(p.policy).Statement : s.Sid => { acoes = toset(s.Action), recursos = s.Resource } } == {
        ConsumirFila = {
          acoes    = toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"])
          recursos = [{ antifraude = var.fila_antifraude, notificacoes = var.fila_notificacoes }[k]]
        }
        Inbox = {
          acoes    = toset(["dynamodb:PutItem", "dynamodb:UpdateItem"])
          recursos = [aws_dynamodb_table.inbox_consumidor[k].arn]
        }
        Logs = {
          acoes    = toset(["logs:CreateLogStream", "logs:PutLogEvents"])
          recursos = ["${aws_cloudwatch_log_group.consumidor[k].arn}:*"]
        }
        XRay = {
          acoes    = toset(["xray:PutTraceSegments", "xray:PutTelemetryRecords"])
          recursos = "*"
        }
      }
      && p.role == aws_iam_role.consumidor[k].id
    ])
    error_message = "A policy de cada consumidor só pode ter a fila, a inbox e o log group DELE (e X-Ray, que não aceita recurso)."
  }

  # As duas instâncias são de fato recursos diferentes (senão o assert acima provaria pouco).
  assert {
    condition = (
      aws_dynamodb_table.inbox_consumidor["antifraude"].arn != aws_dynamodb_table.inbox_consumidor["notificacoes"].arn
      && aws_cloudwatch_log_group.consumidor["antifraude"].arn != aws_cloudwatch_log_group.consumidor["notificacoes"].arn
    )
    error_message = "Inbox e log group são por consumidor."
  }

  assert {
    condition = alltrue([
      for k, r in aws_iam_role.consumidor :
      r.name == "desafio-consumidor-${k}-role"
      && r.permissions_boundary == "arn:aws:iam::123456789012:policy/desafio-zup-limite-de-permissoes"
      && jsondecode(r.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    ])
    error_message = "Role por consumidor, assumida só pelo Lambda e com o permissions boundary."
  }

  # Inbox: chave pk, TTL em `ttl` (30 dias, gravado pelo código), PITR e SSE como as tabelas do módulo dados.
  assert {
    condition = alltrue([
      for k, t in aws_dynamodb_table.inbox_consumidor :
      t.name == "desafio-consumidor-${k}-inbox" && t.hash_key == "pk" && t.billing_mode == "PAY_PER_REQUEST"
      && t.ttl[0].enabled && t.ttl[0].attribute_name == "ttl"
      && t.point_in_time_recovery[0].enabled && t.server_side_encryption[0].enabled
    ])
    error_message = "Inbox por consumidor: pk, TTL no atributo ttl, PITR e SSE."
  }

  assert {
    condition = alltrue([
      for k, g in aws_cloudwatch_log_group.consumidor :
      g.name == "/aws/lambda/desafio-consumidor-${k}" && g.retention_in_days == aws_cloudwatch_log_group.this["relay"].retention_in_days
    ]) && length(aws_cloudwatch_log_group.consumidor) == 2
    error_message = "O log group de cada consumidor tem a mesma retenção dos outros (7 dias no sandbox)."
  }

  assert {
    condition     = output.consumidores_funcoes == { consumidor-antifraude = "desafio-consumidor-antifraude", consumidor-notificacoes = "desafio-consumidor-notificacoes" }
    error_message = "O output consumidores_funcoes alimenta os alarmes lambda-erros-consumidor-<fila>."
  }
}

# --- Módulo: consumidor sem zip é recusado na validação, antes de qualquer recurso -------------------------------
run "consumidores_sem_zip_recusado" {
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
    consumidores = { antifraude = var.fila_antifraude }
  }

  expect_failures = [var.zip_consumidores]
}

# --- Raiz: o sandbox liga antifraude e notificacoes (e não a contabilização) e cria os alarmes de cada um -----------
run "consumidores_no_sandbox" {
  command = plan

  variables {
    account_id         = "123456789012"
    zip_relay          = "versions.tf" # os zips reais vêm de `make pacotes`; no plan basta o arquivo existir
    zip_contabilizacao = "versions.tf"
    zip_consumidores   = "versions.tf"
  }

  override_module {
    target = module.eventos
    outputs = {
      bus_nome = "financas"
      bus_arn  = "arn:aws:events:sa-east-1:123456789012:event-bus/financas"
      filas_arns = {
        contabilizacao = "arn:aws:sqs:sa-east-1:123456789012:contabilizacao"
        antifraude     = "arn:aws:sqs:sa-east-1:123456789012:antifraude"
        notificacoes   = "arn:aws:sqs:sa-east-1:123456789012:notificacoes"
      }
      filas_nomes          = { contabilizacao = "contabilizacao", antifraude = "antifraude", notificacoes = "notificacoes" }
      dlqs_nomes           = { contabilizacao = "contabilizacao-dlq", antifraude = "antifraude-dlq", notificacoes = "notificacoes-dlq" }
      fila_legado_arn      = "arn:aws:sqs:sa-east-1:123456789012:legado-contratos"
      fila_legado_nome     = "legado-contratos"
      fila_legado_dlq_nome = "legado-contratos-dlq"
      entrega_dlq_nome     = "financas-entrega-dlq"
    }
  }

  assert {
    condition = module.lambdas.consumidores_filas == {
      antifraude   = "arn:aws:sqs:sa-east-1:123456789012:antifraude"
      notificacoes = "arn:aws:sqs:sa-east-1:123456789012:notificacoes"
    }
    error_message = "O sandbox liga um consumidor de referência a antifraude e a notificacoes, e nenhum à contabilizacao (que tem o dela)."
  }

  assert {
    condition = module.lambdas.consumidores_inboxes == {
      antifraude   = "desafio-consumidor-antifraude-inbox"
      notificacoes = "desafio-consumidor-notificacoes-inbox"
    }
    error_message = "Cada consumidor de referência tem a inbox dele."
  }

  # Alarmes: Errors de cada função nova (mesmo padrão lambda-erros-<função>) e, nas duas filas, idade da mensagem mais
  # antiga (agora mede atraso de consumo) e mensagens na DLQ.
  assert {
    condition = length(setsubtract([
      "desafio-autorizacao-lambda-erros-consumidor-antifraude",
      "desafio-autorizacao-lambda-erros-consumidor-notificacoes",
      "desafio-autorizacao-idade-da-fila-antifraude",
      "desafio-autorizacao-idade-da-fila-notificacoes",
      "desafio-autorizacao-dlq-antifraude-dlq",
      "desafio-autorizacao-dlq-notificacoes-dlq",
    ], module.observabilidade.alarmes)) == 0
    error_message = "Faltam alarmes dos consumidores: lambda-erros-consumidor-<fila>, idade-da-fila-<fila> ou dlq-<fila>-dlq."
  }
}
