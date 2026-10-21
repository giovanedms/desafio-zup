# Como ler o serviço `contabilizacao` (BC Contabilização)

**Uma frase:** consome os eventos de domínio da autorização pela fila `contabilizacao` e grava, para cada evento contábil, um **lançamento em partidas dobradas imutável**, com a deduplicação (inbox) na **mesma escrita atômica**.

Não tem API HTTP. Na AWS roda como Lambda com a fila como gatilho; no ambiente local, como poller em container. O núcleo é o mesmo, só muda o adapter de entrada (ADR-0004).

## O caminho de um evento (siga nesta ordem)

1. **Entrada**, um adapter por ambiente:
   - **AWS:** `adapters/inbound/lambda_sqs.py → handler`. Event source mapping da fila `contabilizacao`, lotes de 10, `ReportBatchItemFailures` (`infra/terraform/modules/lambdas/main.tf`).
   - **Local:** `adapters/inbound/poller.py → uma_iteracao`. Long poll de 10 s, até 10 mensagens; apaga a mensagem só depois do sucesso. Toca `/tmp/contabilizacao.heartbeat` a cada volta sem exceção (volta que falha não renova), e o `HEALTHCHECK` do Dockerfile lê esse arquivo.
2. **`adapters/inbound/eventbridge_sqs.py → cloudevent_da_mensagem`:** o corpo da mensagem SQS é o envelope do EventBridge, e o `detail` é o CloudEvent. `json.loads(parse_float=Decimal)`: o valor nunca passa por float. Corpo que não é JSON, ou sem `detail` objeto → `EventoInvalido`.
3. **`application/contabilizar.py → ContabilizarEvento.executar`:**
   1. `EventoRecebido.de_cloudevent` (`domain/regras.py`): **leitor tolerante**. Lê só `id`, `source`, `time` e `data`; o nome do evento vem de `data.event_type`. Campo novo não quebra; campo mínimo faltando → `EventoInvalido`.
   2. `LivroDeRegras.lancar`: escolhe a regra pelo nome do evento. Evento sem regra → **`IGNORADO`** (não é contábil) e a mensagem sai da fila.
   3. Chave da inbox = `source|id`, a identidade do CloudEvent.
   4. `RepositorioContabil.registrar(lancamento, chave)` (porta, `Protocol`): `True` → **`CONTABILIZADO`**; `False` → **`DUPLICADO`**.
4. **`domain/regras.py`:** uma regra por evento (Strategy). Evento novo é regra nova registrada, sem tocar nas outras (OCP).
5. **`domain/lancamento.py → Lancamento.__post_init__`:** a invariante é checada antes de o lançamento existir: Σ débitos = Σ créditos, pelo menos uma partida de cada natureza, valores > 0. Senão, `LancamentoDesbalanceado` (bug de regra, nunca dado de entrada).
   - `id_lancamento = UUIDv5(source|id)`: o mesmo evento gera sempre o mesmo lançamento.
   - `data_contabil` é a data **de São Paulo** do `time` do evento (`FUSO_CONTABIL`), não a UTC.
6. **`adapters/outbound/dynamodb.py → RepositorioContabilDynamoDB.registrar`:** **uma** `TransactWriteItems` com dois `Put`, ambos com `attribute_not_exists(pk)`:
   - `INBOX#<source>|<id>` em `contabilizacao-inbox`, com TTL de 30 dias;
   - `LANC#<id_lancamento>` em `contabilizacao-lancamentos`, sem TTL (a retenção contábil é regulatória).

   Transação cancelada por `ConditionalCheckFailed` no primeiro item (a inbox) → `False`. Qualquer outro erro sobe.

## O que cada evento lança

Plano de contas **ilustrativo**, inspirado nos grupos do COSIF (`Conta`, em `domain/lancamento.py`). Em produção, o mapeamento evento → conta é parametrizado pela área contábil.

| Evento | Caso | Débito | Crédito | Valor |
|---|---|---|---|---|
| `TransacaoAutorizada` | `DEBITO` (inclusive a captura) | 1.6.1 Operações de crédito | 4.9.9 Transações a liquidar com estabelecimentos | `valor` |
| `TransacaoAutorizada` | `CREDITO` (pagamento) | 1.1.1 Disponibilidades | 1.6.1 Operações de crédito | `valor` |
| `TransacaoAutorizada` | `RESERVA` | 3.0.1 Compensação ativa | 9.0.1 Compensação passiva | `valor` |
| `TransacaoEstornada` | original `DEBITO` | 4.9.9 | 1.6.1 | `valor` |
| `TransacaoEstornada` | original `RESERVA` | 9.0.1 | 3.0.1 | `valor` |
| `ReservaCapturada` | — | 9.0.1 | 3.0.1 | `valor_reservado` inteiro |
| `ReservaExpirada` | — | 9.0.1 | 3.0.1 | `valor` |

- **Estorno é o lançamento inverso.** O original nunca é editado (`Lancamento` é `frozen`): correção é outro lançamento.
- **Captura parcial são dois lançamentos, um por evento (ADR-0020):** o `TransacaoAutorizada` (DEBITO) lança o valor capturado; o `ReservaCapturada` baixa a compensação pelo valor reservado inteiro, porque a sobra voltou ao limite.
- `tipo_operacao` (ou `tipo_operacao_original`) desconhecido → `EventoInvalido`.
- O bus só encaminha esses quatro tipos para a fila: `assinaturas` em `infra/terraform/modules/eventos/variables.tf`, igual a `ASSINATURAS` em `infra/local/bootstrap.py`.

## Por que um evento nunca é lançado duas vezes

- **Entrega é at-least-once.** Relay e sweeper podem publicar o mesmo evento, e o SQS reentrega. O `id` do CloudEvent é estável (ADR-0007).
- **A inbox entra na mesma transação do lançamento:** ou existem os dois, ou nenhum. A reentrega falha na condição da inbox e volta `DUPLICADO`, que conta como sucesso: a mensagem sai da fila.
- **TTL da inbox (30 dias) maior que a retenção do SQS (14 dias):** nenhuma reentrega possível sobrevive à inbox.
- **`ClientRequestToken` = hash do conteúdo + nonce por chamada.** Ele só protege o retry do SDK depois de um commit incerto. Sem o nonce, uma reentrega em menos de 10 min reaproveitaria o token, o DynamoDB responderia sucesso idempotente e a duplicata seria contada como `CONTABILIZADO` (comentário em `registrar`).
- **Adapter em memória** (`adapters/outbound/memoria.py`): mesma semântica, com um `Lock` no check-and-set.
- **Prova:** `tests/contract/test_porta_repositorio_contabil.py`, a mesma suíte na memória e no DynamoDB (LocalStack), inclusive `test_corrida_de_20_threads_com_o_mesmo_evento_grava_exatamente_um`.

## Ordem de chegada

Cada evento gera um lançamento **autocontido**, e saldo de conta é soma, que é comutativa. Um estorno que chega antes da autorização só lança a reversão; o saldo final é o mesmo em qualquer ordem.

- **Prova:** `tests/unit/test_contabilizacao.py::test_saldo_final_independe_da_ordem_e_das_reentregas` (Hypothesis, 150 exemplos, com reentregas duplicadas misturadas).

## Falhas

| Falha | O que acontece | Arquivo / teste |
|---|---|---|
| Mensagem inválida (não é JSON, sem `detail`, CloudEvent incompleto, tipo desconhecido) | `EventoInvalido`. A Lambda devolve **só o `messageId` dela** em `batchItemFailures`, e o resto do lote segue. O SQS reentrega e, depois de 5 recebimentos (`max_receive_count`), manda para `contabilizacao-dlq` | `test_lambda_sqs.py::test_evento_invalido_reporta_so_o_message_id_dele`, `test_eventbridge_sqs.py` |
| DynamoDB fora ou estrangulado | A exceção sobe e segue o mesmo caminho: item falho, reentrega, DLQ. Reprocessar é seguro por causa da inbox | `lambda_sqs.py`; runbook R1 |
| Regra que gera lançamento desbalanceado | `LancamentoDesbalanceado` antes de gravar; mesmo caminho | `test_lancamento_desbalanceado_e_rejeitado` |
| Exceção no poller local | A mensagem não é apagada; o SQS reentrega depois do visibility timeout | `poller.py` |

## Hexagonal

- `domain/` e `application/` só importam stdlib e o próprio pacote. O `.importlinter` (`contabilizacao-layers` e `contabilizacao-nucleo-puro`) proíbe ali boto3, botocore, pydantic, FastAPI e afins.
- `main.py` é o composition root: liga o adapter DynamoDB ao caso de uso. `cliente_aws` recusa endpoint que não seja local e, com endpoint, usa credenciais fictícias explícitas.
- `config.py` lê `TABELA_LANCAMENTOS`, `TABELA_INBOX`, `AWS_REGION`, `AWS_ENDPOINT_URL` (só local) e `FILA_CONTABILIZACAO`.

## O que não está aqui

- **ACL de saída para o legado contábil:** desenhada e não implementada (ADR-0014, item 7). Os lançamentos ficam na tabela `contabilizacao-lancamentos`.

## Perguntas prováveis e onde está a resposta

| Pergunta | Onde |
|---|---|
| "E se o mesmo evento chegar duas vezes ao mesmo tempo?" | "Por que um evento nunca é lançado duas vezes" + corrida de 20 threads na suíte de contrato da porta |
| "E se o estorno chegar antes da autorização?" | "Ordem de chegada" + property test |
| "O centavo sobrevive ao caminho EventBridge → SQS → consumidor?" | `parse_float=Decimal` na entrada; `tests/integration/test_fluxo_eventbridge_sqs.py` leva `12345678901234.56` do `PutEvents` até o lançamento no DynamoDB (LocalStack) |
| "Qual a data contábil de uma compra às 23h30 em São Paulo?" | A local, não a UTC: `test_data_contabil_e_a_data_de_sao_paulo` |
| "Por que inbox, se o id do lançamento já é determinístico?" | Hoje há as duas proteções. Deduplicar só pelo lançamento e dispensar a inbox é a alavanca 3 de `docs/capacidade.md` §5 (inbox + lançamento custam 4,3 WRU por evento contabilizado; a captura contabiliza 2) |
| "Por que Lambda aqui e Fargate na autorização?" | Trabalho assíncrono, orientado a evento e com rajadas; não precisa do Service Connect (ADR-0004) |
| "Como se corrige um lançamento errado?" | Com outro lançamento: o `Lancamento` é imutável |
