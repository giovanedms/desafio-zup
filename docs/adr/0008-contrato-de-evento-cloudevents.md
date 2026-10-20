# ADR-0008 — Contrato de evento: CloudEvents em `detail`, versionamento e gate de compatibilidade

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** EVT-01, EVT-04, EVT-05, EVT-06, EVT-08

## Contexto

O enunciado pede o evento `TransacaoAutorizada` (v1) "no formato CloudEvents", publicado no EventBridge, com `event_id` para dedup, evolução retrocompatível "validada por um schema registry" e consumidores tolerantes a reordenação.

Fatos (`01-pesquisa/03` e `02`):

- **O envelope do EventBridge não é CloudEvents.** O `id` do envelope é gerado pelo EventBridge e muda a cada `PutEvents`.
- **O Schema Registry do EventBridge não valida eventos nem checa compatibilidade.** Ele só documenta (OpenAPI 3 / JSON Schema draft-04). O Glue Schema Registry checa compatibilidade, mas não se integra ao bus Classic. O novo Custom Event Bus integra com o Glue só para Avro/Protobuf e não existe em sa-east-1.
- **Restrições do CloudEvents:**
  - não tem atributo de versão: o Primer põe o major no `type` e a versão completa no `dataschema`;
  - nomes de atributo não aceitam `_`;
  - a extensão `sequence` vale por `source`.

## Decisão

1. **CloudEvent estruturado inteiro em `detail`**, com `DetailType` = nome curto (`TransacaoAutorizada`) para roteamento por regra.
   - `id` = `event_id` gerado no outbox, estável entre reentregas.
   - `source` = `/financas/autorizacao`.
   - `type` = `example.financas.autorizacao.TransacaoAutorizada.v1`.
   - `dataschema` = URL da versão completa.
   - `subject` e `partitionkey` = `id_contrato`.
   - Extensões `traceparent` (W3C) e `correlationid`.
2. **Campos do enunciado espelhados em `data`** (`event_id`, `event_type`, `event_version`, `occurred_at`, `correlation_id`, `trace_id`), para aderir literalmente ao contrato pedido. Os atributos CE são a fonte; a redundância é intencional e documentada.
3. **Ordem por contrato** = `versao_limite` em `data`, a versão do agregado Limite após a operação. O consumidor descarta versão menor ou igual à última aplicada para aquele contrato. Não usamos a extensão `sequence` porque a nossa ordem é por contrato, não por `source`.
4. **Schemas como código** em `contracts/events/*.v1.schema.json` (JSON Schema draft-04, registrável no Schema Registry do EventBridge para descoberta), com exemplos.
5. **Validação de verdade em três pontos:**
   - teste de contrato no produtor: todo evento gerado valida contra o schema (`autorizacao/tests/contract/test_cloudevents_contrato.py`);
   - **gate de compatibilidade na CI**: dentro do major só entram campos opcionais novos (BACKWARD), comparando o schema do PR com o da `main`;
   - consumidor tolerante: `additionalProperties: true`, ignora campo desconhecido.
6. **Catálogo de eventos:**

   | Evento | BC emissor |
   |---|---|
   | `TransacaoAutorizada` | Autorização |
   | `TransacaoRecusada` (recusa é fato auditável) | Autorização |
   | `TransacaoEstornada` | Autorização |
   | `ReservaExpirada` | Autorização, dono do ciclo de vida |
   | `ReservaCapturada` | Autorização |
   | `LimiteAlterado` (só alteração do limite concedido ou status vinda do core; **não** a cada reserva) | Limites |

## Consequências

- **Positivas:**
  - O contrato é verificável por máquina.
  - Dedup por `(source, id)` funciona mesmo com reentrega do EventBridge.
  - Trocar o bus por Kafka ou Kinesis mantém o evento igual, porque é CloudEvents puro.
- **Negativas:**
  - Redundância entre atributos CE e `data`.
  - O "schema registry" é o repositório versionado mais a CI, e não um serviço AWS que bloqueie publicação. Isso é dito na defesa sem rodeio.

## Quando esta decisão deixa de ser boa

- **Migração para Kafka/MSK ou para o novo Custom Event Bus** (quando chegar a sa-east-1) com Avro/Protobuf: o Glue Schema Registry passa a **bloquear** publicação incompatível, e o gate da CI vira redundante.
- **Mais de ~20 tipos de evento ou vários times publicando:** vale um registry central com dono e um portal de descoberta.

## Referências

- CloudEvents 1.0.2: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md · Primer (versionamento): https://github.com/cloudevents/spec/blob/main/cloudevents/primer.md
- Extensões Distributed Tracing e Partitioning: https://github.com/cloudevents/spec/tree/main/cloudevents/extensions
- EventBridge Schema Registry: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-schema.html
- Glue Schema Registry (modos de compatibilidade): https://docs.aws.amazon.com/glue/latest/dg/schema-registry.html
- W3C Trace Context: https://www.w3.org/TR/trace-context/
