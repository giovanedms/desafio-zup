# ADR-0005 — Reservar limite com escrita condicional de item único, sem transação no item quente

- **Status:** Aceito
- **Data:** 2026-10-19
- **Decisores:** Giovane de Morais (Arquiteto de Aplicação)
- **Requisitos atendidos:** RN-02, MS-03, API-04, API-07, NFR-03

## Contexto

O limite de um contrato é o recurso disputado por todas as autorizações daquele contrato. O enunciado pede "reserva de limite por contrato, com garantia de consistência sob concorrência", 5.000 TPS no pico e `saldo_reservado` no 201 e no evento.

Fatos que restringem a escolha (fontes em `01-pesquisa/02-aws-servicos-e-quotas.md`):

- `TransactWriteItems` concorrentes no mesmo item **cancelam** em vez de esperar (`TransactionConflict`), e o SDK não refaz.
- `TransactWriteItems` não devolve os valores depois do update, só `ReturnValuesOnConditionCheckFailure`.
- Escritas de item único (`UpdateItem`) no mesmo item são **serializadas** pelo DynamoDB: a condição é avaliada contra o estado do instante da escrita.
- Uma partição aguenta 1.000 WCU/s. Um item de ≤ 1 KB suporta ~1.000 escritas/s; numa transação, ~500/s (2× WCU). Num contrato movimentado o item não fica em 1 KB, porque a guarda de idempotência mora nele: o teto real cai para ~8 op/s sustentadas (conta no ADR-0016).
- O DynamoDB Local (base da LocalStack) **não simula** `TransactionConflictException`. Uma demo baseada em transação passaria localmente e falharia na AWS.
- O IND3304 do Itaú (re:Invent 2025, transcrição) usa lock otimista com `TransactWriteItems`. Funciona bem quando a contenção por item é baixa.

## Drivers

1. Nenhuma reserva aprovada pode deixar o disponível negativo. A invariante vale mesmo com 100 requisições simultâneas no mesmo contrato.
2. `saldo_reservado` exato na resposta, sem leitura extra.
3. Vazão por contrato quente. Contratos corporativos concentram tráfego.
4. Commit incerto (timeout com a escrita já aplicada) nunca pode debitar duas vezes.
5. O mesmo comportamento no ambiente local da demo e na AWS.

## Opções consideradas

1. **Lock otimista com `TransactWriteItems`** (ler versão, escrever condicionado à versão, junto com a reserva e o outbox).
2. **Decremento condicional de item único** (`UpdateItem ... SET disponivel = disponivel - :v CONDITION disponivel >= :v`), com a guarda de idempotência no próprio item.
3. **Aurora PostgreSQL** com `UPDATE ... WHERE disponivel >= :v` (lock de linha).
4. **Aurora DSQL** (OCC, conflito no commit).
5. **Serializar por contrato numa fila** (single writer por partição, Kinesis/SQS FIFO), com resposta assíncrona.

## Decisão

**Opção 2.** Toda operação no limite é **um único `UpdateItem` condicional** no item do contrato:

- **Regra no domínio.** O agregado `Limite` gera um `Plano` declarativo (pré-condições + efeitos) sem ler estado (`services/limites/src/limites/domain/operacoes.py`).
- **O adapter compila o plano.** O plano vira `ConditionExpression` + `UpdateExpression` (`adapters/outbound/dynamodb.py::compilar`). A regra mora num lugar só e o banco a executa de forma atômica.
- **Guarda de idempotência no item:**
  - `ops.<id_operacao>`, com **janela de 5 min**. A compactação vai **de carona**: as entradas vencidas vistas na resposta de uma escrita saem no `REMOVE` da escrita seguinte do mesmo contrato, sem escrita extra (`compilar(..., remover_ops)`). O que garante que 5 min bastam é o prazo de reaplicação do chamador (ADR-0006);
  - `reservas.<id>`: o próprio hold, enquanto existir. Ele também é o árbitro entre captura, estorno e expiração (ADR-0020).
- **O retry do SDK é seguro.** Depois de um commit incerto, o retry falha na guarda e o resultado é diagnosticado como `JA_APLICADA`.
- **`ReturnValues=ALL_NEW`** devolve os saldos exatos.
- **Quando a condição falha.** `ReturnValuesOnConditionCheckFailure=ALL_OLD` traz o item antigo, e o **mesmo** `Limite.diagnosticar()` do domínio explica o porquê (já aplicada / limite insuficiente / contrato inativo / crédito excede utilizado / reserva divergente / reserva não encontrada).
- **Estrangulamento não é bug.** `ProvisionedThroughputExceeded`/`Throttling` depois dos retries do SDK vira `ArmazenamentoIndisponivel` → 503 com `Retry-After`, não 500 (o retry é seguro pela guarda).

**Nenhuma transação toca o item de Limite.** Transação aparece só no BC Autorização, em itens sem contenção (ADR-0007).

## Consequências

**Positivas:**
- **Corrida sem 409 de contenção.** 100 reservas paralelas de R$ 100 num limite de R$ 1.000 dão **exatamente 10 aprovadas e 90 recusadas (402)**. É provado pela mesma suíte de contrato nos dois adapters (`tests/contract/test_porta_repositorio.py`, memória e DynamoDB/LocalStack, com threads reais).
- **Retries concorrentes do mesmo `id_operacao` aplicam uma vez só** (50 threads → 1 `APLICADA` + 49 `JA_APLICADA`).
- **Teto por contrato ~2× maior que com transação**, porque não paga o 2× WCU.
- **O domínio é testável sem AWS.** Property-based (Hypothesis) cobre qualquer sequência de operações.

**Negativas / custos aceitos:**
- **A guarda no item aumenta o tamanho dele, e o WCU por escrita cresce com o tamanho.** Isso exige compactação e tem limite prático de holds simultâneos por contrato (ver ADR-0016).
  - **Medido na AWS** antes da compactação existir: depois de um k6 de 60 s, `CTR-CARGA-0001` tinha 246 entradas na guarda e 15 KB (15 WCU por escrita, contra 1). Foi esse número que tirou a compactação do papel.
  - Com a janela de 5 min, a guarda de um contrato fica em ~taxa × 300 entradas de ~45 bytes: 1 op/s → ~13 KB; 10 op/s → ~135 KB. Contrato acima disso é o "contrato quente" do ADR-0016, com alarme próprio.
- **`disponivel` é atributo desnormalizado** (concedido − utilizado − reservado), mantido por todas as escritas. A invariante I1 é verificada nos testes de domínio.
- **Uma operação nunca toca dois contratos atomicamente.** O modelo não precisa: cada autorização é de um contrato só.

**Riscos e mitigação:**

| Risco | Mitigação |
|---|---|
| `ReturnValuesOnConditionCheckFailure` ausente num emulador | Fallback de leitura consistente com retry limitado (`ContencaoPersistente` → 503) |
| Contrato quente (acima de ~8 op/s sustentadas, pelo custo da guarda no item) | Alarme de guarda + reserva fragmentada (ADR-0016) |

## Quando esta decisão deixa de ser boa

- Um contrato precisa de mais de ~8 operações/s sustentadas (teto da partição com a guarda no item, conta no ADR-0016): entra o sharding do limite (ADR-0016) ou o single writer por partição (opção 5).
- Uma operação precisa alterar **dois limites atomicamente** (ex.: transferência entre contratos): aí transação ou saga passam a ser necessárias.
- O banco precisar ser relacional por outro motivo (ex.: consultas analíticas no OLTP): a opção 3 vira candidata, com `UPDATE ... WHERE` condicional e transação curta.

## Alternativas descartadas — por quê

| Opção | Por que não agora |
|---|---|
| 1. Lock otimista com `TransactWriteItems` | Cancela sob contenção (409 em cascata). Não devolve o saldo. Comportamento diferente entre Local e AWS. A solução pública mais madura deste mesmo desafio mediu 2 aprovações em 20 onde cabiam 5 com essa abordagem |
| 3. Aurora PostgreSQL | O lock de linha resolve, mas traz pool de conexões, failover mais lento que o DynamoDB e custo fixo. O ganho só existe se houver necessidade relacional, e o acesso aqui é por chave |
| 4. Aurora DSQL | OCC também aborta no commit sob contenção (`40001`). Em sa-east-1 é só single-Region. Taxa de conexão de 100/s não ajustável |
| 5. Fila por contrato | Garante ordem e elimina contenção, mas transforma a autorização síncrona em assíncrona, o que quebra o contrato da API |

## Referências

- DynamoDB — condition expressions e escritas condicionais: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html
- DynamoDB — transações (conflitos, custo 2×, `ClientRequestToken` de 10 min): https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html
- DynamoDB — `ReturnValuesOnConditionCheckFailure` (UpdateItem): https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_UpdateItem.html
- DynamoDB — limites de partição e hot keys: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html
- AWS Builders' Library — Making retries safe with idempotent APIs: https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
- O'Neil, P. (1986). The Escrow Transactional Method. ACM TODS. https://doi.org/10.1145/7239.7265
