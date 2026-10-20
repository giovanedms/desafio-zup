# Tabelas DynamoDB: espelham EXATAMENTE as definições locais (adapters/outbound/dynamodb.py e
# infra/local/bootstrap.py). On-demand (PAY_PER_REQUEST), PITR em todas.
locals {
  tabelas = {
    # Agregado Limite (item único por contrato, ADR-0005); stream alimenta projeções/limite-alterado.
    limites = {
      hash_key    = "id_contrato"
      stream_view = "NEW_AND_OLD_IMAGES"
      ttl         = false
      gsis        = {}
    }
    # Autorizações + chaves de idempotência (retenção quente de 30 dias via ttl).
    # GSI esparso gsi_orfas (ADR-0006): só itens EM_PROCESSAMENTO têm orfa_shard/orfa_lease_ms, então o índice
    # contém apenas as candidatas a órfã; o reconciliador consulta os N shards (EP#0..EP#N-1) por lease vencido.
    # GSI esparso gsi_vencimentos: só reservas APROVADAS ainda pendentes carregam venc_shard (RV#0..RV#N-1) e venc_ms
    # (expira_em em ms); o expirador consulta os N shards por vencimento, sem Scan. Ao capturar/estornar/expirar,
    # o item é regravado sem os atributos e sai do índice. N (padrão 64) vem de SHARDS_DE_ORFAS/SHARDS_DE_VENCIMENTOS
    # nos serviços: a sort key é monotônica, então cada valor de shard fica preso a 1.000 WCU/s (ADR-0016).
    autorizacoes = {
      hash_key    = "pk"
      stream_view = null
      ttl         = true
      gsis = {
        gsi_orfas       = { hash_key = "orfa_shard", hash_type = "S", range_key = "orfa_lease_ms", range_type = "N" }
        gsi_vencimentos = { hash_key = "venc_shard", hash_type = "S", range_key = "venc_ms", range_type = "N" }
      }
    }
    # Outbox transacional (ADR-0007); o stream NEW_IMAGE alimenta o relay.
    "autorizacoes-outbox" = {
      hash_key    = "pk"
      stream_view = "NEW_IMAGE"
      ttl         = true
      gsis        = {}
    }
    # Lançamentos contábeis (consumidor idempotente).
    "contabilizacao-lancamentos" = {
      hash_key    = "pk"
      stream_view = null
      ttl         = false
      gsis        = {}
    }
    # Inbox de deduplicação da contabilização.
    "contabilizacao-inbox" = {
      hash_key    = "pk"
      stream_view = null
      ttl         = true
      gsis        = {}
    }
  }
}

# Uma tabela por entrada de local.tabelas.
resource "aws_dynamodb_table" "this" {
  #checkov:skip=CKV_AWS_119:Sandbox usa a chave gerenciada pela AWS (SSE padrão); produção usa CMK dedicada por BC.
  for_each = local.tabelas

  name         = "${var.prefixo}${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash_key

  attribute {
    name = each.value.hash_key
    type = "S"
  }

  # Atributos-chave dos GSIs (hash e range de cada índice).
  dynamic "attribute" {
    for_each = merge([
      for g in each.value.gsis : { (g.hash_key) = g.hash_type, (g.range_key) = g.range_type }
    ]...)

    content {
      name = attribute.key
      type = attribute.value
    }
  }

  dynamic "global_secondary_index" {
    for_each = each.value.gsis

    content {
      name            = global_secondary_index.key
      projection_type = "ALL"

      key_schema {
        attribute_name = global_secondary_index.value.hash_key
        key_type       = "HASH"
      }

      key_schema {
        attribute_name = global_secondary_index.value.range_key
        key_type       = "RANGE"
      }

      # Pré-aquecimento do índice (só quando configurado; ver var.warm_throughput).
      dynamic "warm_throughput" {
        for_each = try([var.warm_throughput[each.key].indices[global_secondary_index.key]], [])

        content {
          read_units_per_second  = warm_throughput.value.leitura
          write_units_per_second = warm_throughput.value.escrita
        }
      }
    }
  }

  # Pré-aquecimento da tabela para pico previsível (RN-04, ADR-0013). Desligado por padrão: aumentar é cobrado e
  # o valor NÃO pode ser reduzido depois. Sem o bloco, o provider só lê o valor atual (computed).
  dynamic "warm_throughput" {
    for_each = contains(keys(var.warm_throughput), each.key) ? [var.warm_throughput[each.key]] : []

    content {
      read_units_per_second  = warm_throughput.value.leitura
      write_units_per_second = warm_throughput.value.escrita
    }
  }

  stream_enabled   = each.value.stream_view != null
  stream_view_type = each.value.stream_view

  dynamic "ttl" {
    for_each = each.value.ttl ? [1] : []

    content {
      attribute_name = "ttl"
      enabled        = true
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.deletion_protection

  tags = { Name = "${var.prefixo}${each.key}" }
}

# Contributor Insights nas tabelas quentes: chaves mais acessadas e mais estranguladas (throttled). É o "alarme de
# contrato quente" do ADR-0016: um id_contrato dominando o ranking indica hot partition antes do throttling virar incidente.
resource "aws_dynamodb_contributor_insights" "this" {
  for_each = toset(["limites", "autorizacoes"])

  table_name = aws_dynamodb_table.this[each.key].name
}
