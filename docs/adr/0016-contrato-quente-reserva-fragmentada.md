# ADR-0016 — Contrato quente: teto por item e reserva fragmentada (escrow) como evolução

- **Status:** Aceito (desenho); implementação como evolução quando o gatilho disparar
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-02 (limite de escala), NFR-03

## Contexto

Com um item por contrato (ADR-0005), toda operação de um contrato cai na mesma partição do DynamoDB, que aguenta **1.000 WCU/s**. Cada `UpdateItem` custa ⌈maior tamanho do item (antes ou depois) ÷ 1 KB⌉ WCU, e a guarda de idempotência mora no item.

**A conta que manda (corrigida em 20/10, depois da medição na AWS):** com taxa sustentada *r* op/s por contrato e janela *W* = 300 s, a guarda tem ~*r*·*W* entradas de ~45 bytes (UUID de 36 caracteres + instante). O custo por escrita cresce com *r*, e o custo total com *r*²:

| *r* (op/s no contrato) | guarda | WCU por escrita | WCU/s no contrato |
|---|---|---|---|
| 0,01 (pessoa física típica) | ~3 entradas | 1 | 0,01 |
| 1 | ~300 (13 KB) | 14 | 14 |
| 5 | ~1.500 (66 KB) | 66 | 330 |
| **~8,7** | ~2.600 (115 KB) | 115 | **~1.000 (teto da partição)** |

**Teto prático do item único: ~8 op/s sustentadas por contrato** (rajadas curtas passam, porque a guarda leva 5 min para encher). A primeira versão deste ADR dizia "300–1.000 op/s": essa conta ignorava a guarda. Para 80 M de contas a 5.000 TPS a média por contrato é de milésimos de op/s e o limite sobra; o problema é só o contrato corporativo quente (frota de cartões de uma empresa grande, por exemplo).

## Decisão

1. **Hoje:** item único por contrato, com três controles, todos no código e no Terraform:
   - **compactação de carona** da guarda de idempotência: entradas com mais de 5 min saem no `REMOVE` da escrita seguinte do contrato, sem escrita extra (`limites/adapters/outbound/dynamodb.py::compilar`; teste `test_guarda_de_idempotencia_fica_limitada_a_janela`);
   - **métrica de tamanho da guarda**: escrita que deixa a guarda com 200 entradas ou mais emite uma linha JSON (`evento=guarda_de_idempotencia`, `id_contrato`, `operacoes_na_guarda`). Um metric filter transforma isso na métrica `desafio-zup/limites OperacoesNaGuarda`, com alarme em 2.000 entradas (~90 KB, longe dos 400 KB do item);
   - **contrato quente por chave**: Contributor Insights do DynamoDB nas tabelas `limites` e `autorizacoes` (chaves mais acessadas e mais estranguladas) e uma regra de Contributor Insights sobre a linha JSON acima (top contratos por guarda). A regra de log só agrega por `Sum` ou `Count` (não há `Max`), então ela ranqueia contrato por volume de linhas × tamanho; o valor exato vem da métrica.
   - Estrangulamento que sobrar vira 503 com `Retry-After` (ADR-0005), não 500.
2. **Evolução desenhada — reserva fragmentada (escrow, O'Neil 1986):**
   - o limite do contrato quente é dividido em **N sub-limites** (itens `id_contrato#k`, k = 0..N−1), com N ∝ vazão necessária;
   - a reserva tenta um sub-limite escolhido por hash do `id_operacao` (determinístico, para o retry cair no mesmo) e, se insuficiente, tenta os vizinhos (no máximo N tentativas);
   - um **rebalanceador** assíncrono move saldo entre sub-limites quando a distribuição degenera, com transação de 2 itens fora do caminho quente;
   - o `saldo_reservado` do contrato vira soma dos sub-limites. É exato para auditoria, mas a resposta síncrona passa a trazer o retrato do sub-limite ou uma soma ligeiramente defasada. **Essa é a concessão de contrato.**
   - **Custo honesto:** pode haver **recusa falsa**, quando cada sub-limite sozinho não cobre o valor mas a soma cobriria. Mitigação: valores acima de 1/N do concedido vão para o caminho "consolidado", que reserva em vários sub-limites numa transação.
3. **A escolha da estratégia por contrato é de domínio** (atributo `estrategia_de_reserva`). Uma estratégia nova é uma implementação nova da mesma porta (OCP), e a suíte de contrato da porta vale para ela.

**Alavancas antes de fragmentar**, todas compatíveis com o desenho atual:

- id da guarda em base64url de 16 bytes (22 caracteres em vez de 36): ~35% menos item, teto ~10 op/s;
- janela menor: o piso é o prazo do reconciliador (lease 10 s + graça 60 s + ciclo 30 s ≈ 100 s); com *W* = 150 s o teto vai a ~12 op/s;
- a fragmentação abaixo multiplica o teto por N (cada sub-limite tem a própria guarda).

## Consequências

- **Positivas:** a simplicidade e a exatidão do item único servem o caso comum, e a rota de escala está desenhada, sem reescrita.
- **Negativas:** quando ativada, a fragmentação introduz recusa falsa, um rebalanceador e saldo agregado eventualmente consistente.

## Quando esta decisão deixa de ser boa (gatilhos para ativar a fragmentação)

- Algum contrato com > 5 operações/s **sustentadas**, ou throttling por chave recorrente (Contributor Insights).
- Alarme `OperacoesNaGuarda` disparando: guarda acima de 2.000 entradas depois da compactação, o que equivale a ~7 op/s sustentadas com janela de 5 min.

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Fila por contrato (single writer) | Torna a autorização assíncrona |
| Cache de saldo com reconciliação posterior | Permite overdraft sob concorrência, o que viola RN-02 |
| Banco relacional com lock de linha | O teto por linha quente também existe, e com lock é pior |

## Referências

- O'Neil, P. (1986). The Escrow Transactional Method. ACM TODS 11(4). https://doi.org/10.1145/7239.7265
- DynamoDB — write sharding: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-sharding.html
- CloudWatch Contributor Insights para DynamoDB: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/contributorinsights_HowItWorks.html
