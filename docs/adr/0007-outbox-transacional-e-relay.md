# ADR-0007 — Transactional outbox com relay próprio (mesmo código local e AWS) e sweeper

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-03, EVT-05, RN-06

## Contexto

Toda autorização concluída precisa virar evento para contabilidade, antifraude, notificações e data platform. Gravar no banco e depois publicar no EventBridge é **dual-write**: se o processo cair entre os dois, o evento se perde, e o contrário publica um fato que não existe. Fatos:

- DynamoDB Streams retém 24 h e entrega cada modificação uma vez, em ordem por item.
- EventBridge Pipes lê Streams e publica no bus sem código, mas **não tem ack de volta**: se ficar parado mais de 24 h, o stream expira e o evento some.
- A LocalStack Community 4.14 não emula Pipes.

## Decisão

1. **Outbox na mesma escrita atômica do estado.**
   - `TransactWriteItems` grava a autorização concluída **e** um item `OUTBOX#<id_evento>` por evento na tabela `autorizacoes-outbox` (ADR-0006).
   - O item carrega o CloudEvent já serializado (`adapters/outbound/cloudevents.py`).
2. **Relay próprio** (`relay/`), em vez de Pipes:
   - lê o Stream da tabela de outbox (só `INSERT`);
   - publica em lote com `PutEvents` (até 10 por chamada) no bus custom `financas`;
   - **apaga** o item do outbox depois do sucesso.
   - Na AWS roda como **Lambda** com gatilho no Stream (resposta parcial em lote). Localmente, como **container** que faz polling no Stream da LocalStack. **É o mesmo módulo**, e só o adapter de entrada muda.
3. **Sweeper** periódico. Lê o outbox, que só tem itens pendentes e por isso é pequeno, e republica o que estiver lá há mais de 15 min. Cobre o relay parado e o Stream expirado.
4. **At-least-once, sem dedup na publicação.** Um evento pode sair duas vezes (relay e sweeper, ou retry). O `id` do CloudEvent é estável, e os consumidores deduplicam por `(source, id)` (inbox, ADR-0009).

## Consequências

- **Positivas:**
  - Nenhum evento perdido nem fantasma.
  - Comportamento local idêntico ao da AWS (o mesmo relay), sem divergência Pipes × emulador.
  - O outbox vazio é sinal de saúde: dá para alarmar por tamanho e idade do item mais velho.
- **Negativas:**
  - Código próprio para manter, onde o Pipes seria só configuração.
  - Uma escrita a mais por evento (PutItem do outbox e o Delete).
  - Duplicatas são normais, então todo consumidor precisa ser idempotente.

## Quando esta decisão deixa de ser boa

- **O Pipes ganhar ack ou DLQ com redrive no Stream** e houver confiança operacional: troca-se o relay por configuração e o sweeper continua como rede de segurança.
- **O volume exigir ordem estrita por contrato fim a fim:** o relay publica em Kinesis/MSK particionado por `id_contrato` (ADR-0009) em vez de no bus Classic, que entrega em ordem arbitrária.

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Publicar direto no EventBridge depois de gravar | Dual-write |
| Streams na própria tabela de autorizações (CDC puro) | Funciona, mas o evento seria derivado de diff de estado, e o sweeper teria que varrer uma tabela de centenas de milhões de itens |
| EventBridge Pipes | Sem ack (perda depois de 24 h parado), sem emulação local; fica como alternativa documentada |
| Kinesis Data Streams for DynamoDB (retenção de até 365 dias) | Resolve a retenção, mas pode reordenar ou duplicar e adiciona um serviço. Sweeper + outbox pequeno resolve com menos peças |

## Referências

- microservices.io — Transactional outbox: https://microservices.io/patterns/data/transactional-outbox.html
- AWS Prescriptive Guidance — Transactional outbox pattern: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
- DynamoDB Streams: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html
- EventBridge PutEvents: https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutEvents.html
