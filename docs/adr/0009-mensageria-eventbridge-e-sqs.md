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

Fatos em sa-east-1 (da documentação oficial da AWS; os links principais estão em Referências):

| Serviço | Fato relevante |
|---|---|
| EventBridge Classic | PutEvents 600/s padrão (**6.000 aprovados na conta**), Invocations 1.100/s (**20.000 aprovados**); entrega **sem ordem**, at-least-once; retry de 24 h / 185 tentativas |
| SNS | Publish: **300 mensagens/s por conta** em sa-east-1 nos tópicos Standard (3.000 nos FIFO), somando `Publish` e `PublishBatch` de todos os tópicos da conta; quota ajustável |
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
- **Entrega regra → fila sem perda silenciosa:** todo target tem `retry_policy` explícita (até 1 h ou 185 tentativas) e `dead_letter_config` para a DLQ de entrega `financas-entrega-dlq` (SSE, alarme). Sem isso, a entrega que esgota as tentativas, ou que falha por permissão (o EventBridge nem tenta de novo), seria descartada depois que o outbox já apagou o item. A queue policy da DLQ só aceita `events.amazonaws.com` vindo das regras do bus (`aws:SourceArn`). Cada mensagem traz `RULE_ARN`, `TARGET_ARN` e `ERROR_CODE`, e o redrive vai para a fila do `TARGET_ARN` (runbook R1).
- **Ordem e duplicata ficam com o consumidor:**
  - idempotente por `(source, id)` do CloudEvent, com inbox na mesma escrita do efeito;
  - tolera reordenação, por construção (contabilização: lançamentos autocontidos, saldo comutativo, ver property test) ou por versão (`versao_limite` por contrato, ADR-0008).
- **Data platform:** archive mais Firehose → S3 na produção. No sandbox, só o archive.

## Avaliação das alternativas para alta volumetria e ordem

| Opção | Ordem por chave | Vazão em sa-east-1 | Custo e operação | Veredito |
|---|---|---|---|---|
| **EventBridge + SQS** (escolhida) | Não (consumidor tolera) | 6.000 PutEvents/s aprovados; ajustável | Pago por uso, zero operação | **Agora** |
| SNS → SQS | Não (FIFO sim, com limites) | 300 msg/s padrão por conta (Standard); ajustável | Pago por uso | Avaliado abaixo (regra → SNS → N filas): ganha invocations, perde filtro e replay por consumidor |
| SQS FIFO (`MessageGroupId` = contrato) | Sim, por grupo | 4.500 TPS sem batch | Pago por uso | Bom para **um** consumidor que precise de ordem estrita por contrato |
| Kinesis Data Streams | Sim, por partition key | Escala por shard (on-demand) | Pago por shard-hora | Bom para data platform e antifraude em stream, com replay de dias |
| MSK (Kafka) | Sim, por partição | Muito alta | Piso de cluster provisionado, operação de Kafka | Quando a plataforma do banco já opera Kafka como backbone (o Itaú usa Kafka em casos públicos) |

### Regra → SNS → N filas: por que fica de fora (e quando entra)

O padrão alternativo é uma regra por evento entregando num tópico SNS, com uma assinatura SQS por consumidor. **O ganho é real:** 1 invocation por evento, em vez de uma por regra que casa. No pior mix do pico (toda aprovação é captura, `docs/capacidade.md` seção 1.1) as invocations cairiam de ~21.000/s para ~10.500/s, abaixo da quota aprovada de 20.000. **O que se perde:**

- **filtro por consumidor no EventBridge:** cada consumidor hoje escolhe os `detail-type` na regra dele, com o archive e as métricas por regra. Com SNS, o filtro vai para a filter policy da assinatura, outra sintaxe e outro lugar para auditar;
- **replay por consumidor:** o replay do archive escolhe as regras de destino. Com uma regra só na frente do SNS, um replay para a contabilização reentrega para todas as filas. Os consumidores são idempotentes, então não quebra, mas vira ruído e custo;
- **mais um salto e mais um par retry/DLQ:** regra → SNS (DLQ do target) e SNS → SQS (redrive policy da assinatura), dois lugares para falhar e monitorar;
- **outra quota a pedir, e maior:** o EventBridge entrega no SNS via `Publish`, e a quota de mensagens publicadas do SNS em sa-east-1 é de 300/s por conta nos tópicos Standard, somando todos os tópicos e contando mensagem, não chamada (o `PublishBatch` não alivia). O pico pede ~10.500/s, um aumento de ~35×, contra 20.000 → 25.000 nas invocations (o pedido que já foi aprovado uma vez saiu de 1.100 para 20.000). A quota do SNS também é ajustável: o argumento é o tamanho do aumento a pedir, não um teto que impeça o desenho.

**Fica de fora** porque a quota de invocations é ajustável e o aumento necessário é pequeno, enquanto o filtro e o replay por consumidor valem mais para o consumidor contábil. **Entra** se o aumento de invocations for negado, ou se o número de consumidores por evento crescer: cada consumidor novo de `TransacaoAutorizada` soma ~5.000 invocations/s no pico. Nesse caso, o SNS entra **só** para os eventos de fan-out largo (antifraude, notificações, data platform) e a contabilização mantém a regra própria, com filtro e replay.

## Consequências

- **Positivas:** acoplamento mínimo (o produtor não conhece consumidores), roteamento declarativo, isolamento de falha por fila e custo zero ocioso.
- **Negativas:**
  - Sem ordem nativa: todo consumidor precisa ser idempotente e tolerante a reordenação. É exatamente o que o enunciado pede, então é requisito e não defeito.
  - A captura publica **2** eventos (`TransacaoAutorizada` + `ReservaCapturada`) e casa 4 regras. No pior mix do pico isso dá ~10.500 entradas de PutEvents/s e ~21.000 invocations/s, acima das quotas aprovadas (6.000 e 20.000): é preciso pedir 12.000 e 25.000 antes do pico (conta em `docs/capacidade.md`, seção 1.1).

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Consumidor exige **ordem estrita por contrato** e não consegue usar a versão | Pipe ou relay publicando em SQS FIFO ou Kinesis com `partitionkey` = `id_contrato` (o CloudEvent já carrega a extensão) |
| Volume passa de ~10 k eventos/s sustentados, ou o banco padroniza Kafka | MSK como backbone, com EventBridge Pipes ou o relay publicando lá |
| Novo Custom Event Bus chega a sa-east-1 | Reavaliar: FIFO por `EventGroupId` e dedup nativos simplificariam os consumidores |

## Referências

- EventBridge quotas: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-quota.html
- SNS quotas ("Publish API throttling per account": sa-east-1 está em "All other supported Regions", 300 mensagens/s Standard e 3.000 FIFO; consultado em 21/10/2026): https://docs.aws.amazon.com/general/latest/gr/sns.html
- SQS FIFO high throughput: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/high-throughput-fifo.html
- SQS quotas (FIFO de alta vazão em sa-east-1: 4.500 TPS sem batch, 45.000 com batch): https://docs.aws.amazon.com/general/latest/gr/sqs-service.html
- Kinesis quotas: https://docs.aws.amazon.com/streams/latest/dev/service-sizes-and-limits.html
- microservices.io — Idempotent consumer: https://microservices.io/patterns/communication-style/idempotent-consumer.html
