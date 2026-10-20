# ADR-0009 — EventBridge (roteamento) + SQS por consumidor; SNS, SQS FIFO, Kinesis e MSK avaliados

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** EDA-01, EDA-02, EVT-02, EVT-03, EVT-07

## Contexto

Os eventos de autorização vão para consumidores com necessidades diferentes:

- contabilidade: todo evento contábil, sem perda;
- antifraude: autorizadas e recusadas, com baixa latência;
- notificações;
- data platform.

O enunciado pede EventBridge, SNS e SQS para fan-out e pede para avaliar SQS, Kinesis e MSK para alta volumetria e ordem por chave.

Fatos em sa-east-1 (`01-pesquisa/02`):

| Serviço | Fato relevante |
|---|---|
| EventBridge Classic | PutEvents 600/s padrão (**6.000 aprovados na conta**), Invocations 1.100/s (**20.000 aprovados**); entrega **sem ordem**, at-least-once; retry de 24 h / 185 tentativas |
| SNS Standard | Publish **300 msg/s** padrão |
| SQS FIFO high-throughput | 4.500 TPS por ação sem batch (45.000 com batch) |
| Kinesis | Ordem por partition key; até 1 MB/s ou 1.000 registros/s por shard; retenção de até 365 dias |
| MSK | Só Provisioned em sa-east-1 (sem Serverless). O piso de ~US$ 894/mês da pesquisa é **us-east-1, Express**; o de sa-east-1 não foi verificado |
| Novo Custom Event Bus (24/09/2026) | FIFO por `EventGroupId`, dedup, retenção — **não existe em sa-east-1** |

## Decisão

- **Bus custom `financas` (EventBridge Classic)**, alimentado pelo relay do outbox (ADR-0007).
  - **Regras por `source` + `detail-type`** fazem o roteamento, e cada consumidor só assina o que precisa:

    | Consumidor | Assina |
    |---|---|
    | Contabilização | Autorizada, Estornada, ReservaExpirada, ReservaCapturada |
    | Antifraude | Autorizada, Recusada |
    | Notificações | Autorizada, Recusada, Estornada, LimiteAlterado |

  - **Archive no bus**, para replay.
- **Uma fila SQS Standard por consumidor**, com DLQ (`maxReceiveCount` 5, retenção de 14 dias). A fila é o **amortecedor**: o consumidor parado ou lento não afeta o produtor nem os outros consumidores.
- **Ordem e duplicata ficam com o consumidor:**
  - idempotente por `(source, id)` do CloudEvent, com inbox na mesma escrita do efeito;
  - tolera reordenação, por construção (contabilização: lançamentos autocontidos, saldo comutativo, ver property test) ou por versão (`versao_limite` por contrato, ADR-0008).
- **Data platform:** archive mais Firehose → S3 na produção. No sandbox, só o archive.

## Avaliação das alternativas para alta volumetria e ordem

| Opção | Ordem por chave | Vazão em sa-east-1 | Custo e operação | Veredito |
|---|---|---|---|---|
| **EventBridge + SQS** (escolhida) | Não (consumidor tolera) | 6.000 PutEvents/s aprovados; ajustável | Pago por uso, zero operação | **Agora** |
| SNS → SQS | Não (FIFO sim, com limites) | 300 msg/s padrão | Pago por uso | Sem ganho: o EventBridge já faz fan-out por regra, com filtro por conteúdo e archive |
| SQS FIFO (`MessageGroupId` = contrato) | Sim, por grupo | 4.500 TPS sem batch | Pago por uso | Bom para **um** consumidor que precise de ordem estrita por contrato |
| Kinesis Data Streams | Sim, por partition key | Escala por shard (on-demand) | Pago por shard-hora | Bom para data platform e antifraude em stream, com replay de dias |
| MSK (Kafka) | Sim, por partição | Muito alta | Piso de cluster provisionado, operação de Kafka | Quando a plataforma do banco já opera Kafka como backbone (o Itaú usa Kafka em casos públicos) |

## Consequências

- **Positivas:** acoplamento mínimo (o produtor não conhece consumidores), roteamento declarativo, isolamento de falha por fila e custo zero ocioso.
- **Negativas:**
  - Sem ordem nativa: todo consumidor precisa ser idempotente e tolerante a reordenação. É exatamente o que o enunciado pede, então é requisito e não defeito.
  - A quota de Invocations (20.000) fica perto do teto no pico de 5.500 eventos × 3 regras.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Consumidor exige **ordem estrita por contrato** e não consegue usar a versão | Pipe ou relay publicando em SQS FIFO ou Kinesis com `partitionkey` = `id_contrato` (o CloudEvent já carrega a extensão) |
| Volume passa de ~10 k eventos/s sustentados, ou o banco padroniza Kafka | MSK como backbone, com EventBridge Pipes ou o relay publicando lá |
| Novo Custom Event Bus chega a sa-east-1 | Reavaliar: FIFO por `EventGroupId` e dedup nativos simplificariam os consumidores |

## Referências

- EventBridge quotas: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-quota.html
- SQS FIFO high throughput: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/high-throughput-fifo.html
- Kinesis quotas: https://docs.aws.amazon.com/streams/latest/dev/service-sizes-and-limits.html
- microservices.io — Idempotent consumer: https://microservices.io/patterns/communication-style/idempotent-consumer.html
