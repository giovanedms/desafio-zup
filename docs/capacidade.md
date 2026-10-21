# Capacidade, desempenho e custo

## 1. Dimensionamento (de onde vêm os números)

| Grandeza | Conta | Valor |
|---|---|---|
| Itens de Limite | 80 M contas × 3–5 contratos | 240–400 M itens (~100–160 GB) |
| Requisições em voo no pico | Lei de Little: 5.000 TPS × ~50 ms | ~250 |
| Escritas por operação | **Medido na AWS só no DEBITO** (a carga da seção 3): `limites` 1,09 + `autorizacoes` 3,06 + `autorizacoes-outbox` 6,12 + contabilização 2,14 + 2,14. Os outros tipos são **derivados** do código (seção 1.6) | **~14,6 WRU** por DEBITO nas tabelas, mais 2 nos GSIs. Captura ~27 (2 eventos), recusa ~10 (a estimativa inicial, ~6–8, esquecia o outbox e a contabilização) |
| WRU no pico | 5.000 aprovações + 500 recusas/s, por mix (seção 1.6) | ~89 k (só DEBITO) a ~156 k WRU/s (toda aprovação é captura), tabelas e GSIs somados. O `autorizacoes-outbox` vai a ~34 k, ~49 k e ~64 k/s: **acima dos 40 k por tabela da quota padrão nos dois mixes com captura** |
| Eventos no pico | Depende do mix: captura publica **2** eventos (seção 1.1) | 5,5 k a **10,5 k** entradas de PutEvents/s (quota aprovada: **6.000**; estoura com mais de 10% de capturas) |
| Invocations no pico | Regras casadas por evento (seção 1.1) | 16 k a **21 k**/s sem a data platform (quota aprovada: **20.000**; estoura com mais de 80% de capturas). Com a regra da data platform (+1 por evento): **21,5 k a 31,5 k**/s, acima da quota já no DEBITO |
| Teto por contrato | 1.000 WCU/s por partição, com a guarda de 5 min no item (o custo por escrita cresce com a taxa) | ~8 op/s sustentadas (conta no ADR-0016) |
| Teto por chave dos GSIs esparsos | 2 escritas no `gsi_orfas` por autorização, divididas por N shards; sort key monotônica (seção 1.2) | N = 10: **~1.000 WCU/s por chave, 100% do teto**. N = 64 (padrão agora): ~156 |
| Item do limite com reservas | Holds de até 7 dias moram no item; ~45 bytes cada (seção 1.3) | ~9.000 reservas ativas = 400 KB (teto do item) |
| API Gateway | 10.000 RPS / burst 5.000 por conta e região, somando todas as APIs da conta | O pico usa ~50% (55% com as recusas). Por consumidor: seção 1.4 |

### 1.1 Barramento: eventos e invocations por chamada

O relay publica **um item de outbox = uma entrada de PutEvents**. Cada entrada vira uma invocation por regra que casa (`modules/eventos/variables.tf`):

| Fluxo (uma chamada da API) | Eventos publicados | Regras casadas (invocations) |
|---|---|---|
| DEBITO, CREDITO ou RESERVA aprovados | 1 (`TransacaoAutorizada`) | 3 (contabilização, antifraude, notificações) |
| Recusa (qualquer tipo) | 1 (`TransacaoRecusada`) | 2 (antifraude, notificações) |
| **Captura** de reserva | **2** (`TransacaoAutorizada` + `ReservaCapturada`, na mesma transação) | **4** (3 + contabilização) |
| Estorno | 1 (`TransacaoEstornada`) | 2 (contabilização, notificações) |
| Expiração de reserva (assíncrona, sem chamada) | 1 (`ReservaExpirada`) | 1 (contabilização) |
| Compensação, reconciliação manual | 1 | 0 (só archive) |

A tabela acima não conta a **data platform**: a regra `para-plataforma-de-dados` (módulo `plataforma_de_dados`, ADR-0009) casa todo evento dos dois sources, então cada entrada de PutEvents soma 1 invocation, inclusive a compensação e a reconciliação manual.

No pico de 5.000 aprovações/s + ~500 recusas/s:

| Mix | Entradas de PutEvents/s | Invocations/s | Invocations/s com a data platform |
|---|---|---|---|
| Só DEBITO (a conta antiga) | 5.500 | 16.000 | 21.500 |
| Cartão: metade reserva, metade captura | 8.000 | 18.500 | 26.500 |
| **Pior caso: toda aprovação é captura** (reservas feitas antes do pico) | **10.500** | **21.000** | **31.500** |

**As quotas aprovadas não bastam.** Com uma fração *f* das aprovações sendo captura, são 5.500 + 5.000·*f* entradas/s e 16.000 + 5.000·*f* invocations/s: PutEvents 6.000 estoura a partir de *f* = 10%; Invocations 20.000 aguenta o mix de cartão usando 92% da quota e estoura a partir de *f* = 80%. Com a data platform ligada, as invocations passam a 21.500 + 10.000·*f*: acima dos 20.000 aprovados já no mix só de DEBITO. **Pedir: PutEvents 12.000/s e Invocations ~37.000/s** (pior caso com a data platform + ~15–20%; sem ela, 25.000 bastariam), **e a quota de WRU por tabela do DynamoDB, ~75.000/s**, que o `autorizacoes-outbox` estoura a partir de ~21% de capturas (seção 1.6). O Firehose Direct PUT da data platform tem quota própria por stream: fora de us-east-1, us-west-2 e eu-west-1, **1.000 requisições/s e 1 MiB/s** por padrão (https://docs.aws.amazon.com/firehose/latest/dev/limits.html). O pico passa disso com folga: ~5.500 eventos/s de pelo menos ~1,1 KB cada (o CloudEvent do outbox, sem o envelope do EventBridge) são ≥ ~6 MB/s (estimativa). Pedir o aumento antes do pico; a AWS também sobe o limite sozinha sob throttling, sem prazo garantido. Por isso a regra fica desligada na janela de medição (`plataforma_de_dados_ativa = false`, `terraform.tfvars.example`). A tabela de quotas da AWS fala em requisições de PutEvents por segundo e não diz se uma requisição de 10 entradas (o lote do relay) conta 1 ou 10: a conta acima usa a leitura conservadora (entradas). O fan-out por SNS cortaria as invocations (ADR-0009), com outros custos.

**Medido em 21/10 (seção 3.1), sem a regra da data platform:** a ~4.770 aprovações/s só de DEBITO (média dos minutos de patamar), as 3 regras somaram ~14.300 invocations/s (derivado: 3 × aceitas/s), com 0 falhas e 0 estranguladas. As quotas aprovadas bastaram para esse mix; com a data platform seriam 4 × 5.000 = 20.000, no limite.

**Lambdas do caminho assíncrono.** A concorrência da conta (1.000) é compartilhada. O consumidor contábil tem teto de **200** no ESM (`concorrencia_maxima_sqs`): com lotes de ~100 ms, ~10.000 eventos/s, o dobro do pior mix (estimativa). **A medição de 21/10 não confirmou a estimativa:** com 200, o consumidor ficou para trás a ~4.770 eventos/s, a fila `contabilizacao` chegou a 335 s de atraso e drenou depois do teste (seção 3.1); a duração por lote e a concorrência atingida não estão naquela coleta. Os consumidores de referência de antifraude e notificações (`services/consumidores`) têm teto de **100** cada (`concorrencia_maxima_consumidores`), pela mesma conta e com a mesma ressalva; somados aos 200 da contabilização, 400 das 1.000 da conta. Relay e CDC usam uma execução por shard de Stream (o número de shards a 5.000 TPS não foi medido). O CDC do `LimiteAlterado` lê **todo** registro do Stream da tabela `limites`, porque toda operação incrementa a `versao` e gera um `MODIFY`: ~5.000 registros/s no pico para publicar quase nada (só mudança de `concedido` ou `status` vira evento). A correção estrutural é um outbox próprio do `limites` no `ATUALIZADO` (ADR-0014). O relay filtra `INSERT` no ESM e não é mais invocado pelos `REMOVE` que ele mesmo gera.

### 1.2 GSIs esparsos: a conta por chave de partição

Toda autorização entra no `gsi_orfas` ao ser registrada (`EM_PROCESSAMENTO`) e sai ao concluir: **2 escritas no índice**, de ~1 WCU cada (item de ~0,5–0,7 KB, projeção `ALL`). A 5.000 TPS são ~10.000 WCU/s no índice. A sort key (`orfa_lease_ms`, e `venc_ms` no `gsi_vencimentos`) cresce com o tempo, então o DynamoDB não divide uma chave quente por faixa de sort key, e cada valor de shard fica preso a **1.000 WCU/s**:

| Shards (N) | WCU/s por chave a 5.000 TPS | Ocupação do teto |
|---|---|---|
| 10 (entregue até 20/10) | ~1.000 | **100%**: o índice estrangula e devolve a pressão à escrita da tabela |
| 64 (padrão agora) | ~156 | ~16%; o dobro do pico ainda fica em ~31% |

O `gsi_vencimentos` recebe até ~5.000 escritas/s no mix de cartão (entra na reserva, sai na captura): ~78 WCU/s por chave com N = 64. N vem de `SHARDS_DE_ORFAS`/`SHARDS_DE_VENCIMENTOS` (Terraform: `var.shards_dos_indices`). O reconciliador consulta os N shards dos dois índices a cada 30 s: 128 Querys por rodada, ~2 RRU/s com o índice vazio. **Subir N é retrocompatível** (o leitor consulta 0..N−1, que contém os shards antigos; o item muda de shard na próxima escrita), desde que o leitor suba junto ou antes. **Reduzir N esconde itens**: não fazer sem migrar.

### 1.3 Item do limite: reservas de 7 dias

Cada reserva aprovada e pendente é uma entrada no mapa `reservas` **dentro do item do contrato** (UUID de 36 caracteres + valor, ~45 bytes) e fica lá até a captura, o estorno ou a expiração, em até **7 dias**. A guarda vive 5 min; o hold vive 2.016 vezes mais. Pela Lei de Little, reservas ativas = taxa de reservas do contrato × tempo médio até a captura:

| Contrato | Reservas ativas | Item |
|---|---|---|
| Pessoa física: 1 reserva/dia, capturada em 2 dias | ~2 | < 1 KB |
| Hotel ou posto corporativo: 300 reservas/dia, capturadas em 3 dias | ~900 | ~40 KB, ~40 WCU por escrita |
| Frota: 1.300 reservas/dia que esperam os 7 dias | ~9.100 | **~400 KB: a escrita falha** (`ValidationException`, contrato parado) |

O tamanho do item também baixa o teto de operações do contrato: com 2.000 reservas (~90 KB) cada escrita custa ~90 WCU, e a partição aguenta ~11 op/s. Controles: o log `guarda_de_idempotencia` passa a sair quando guarda **ou** reservas passam de 200 entradas, e o alarme `reservas-ativas-no-item` dispara acima de 2.000 por contrato (~90 KB, 22% do teto). A saída para esse contrato é a fragmentação do ADR-0016, que divide também as reservas.

### 1.4 Borda: governança por consumidor a 5.000 TPS

Os tetos por consumidor da IaC são de demonstração: o `plano-padrao` dá 200 rps e 1 M/dia, e o WAF corta em 120.000 requisições por `x-api-key` em 5 min (400 rps). Nenhum chega perto de 5.000 TPS. A regra do ADR-0010 escala; os números, não. Em produção, para N canais com pico *p* cada (somando 5.000):

| Grandeza | Regra | 3 canais iguais (os do C4 nível 1: cartões, Pix/crédito direto, digitais), *p* ≈ 1.667 |
|---|---|---|
| Taxa do usage plan | 1,6 × *p*: a mesma folga do compute (ADR-0004). Quem passa da própria fatia leva 429 do plano, em vez de levar todos ao 503 do shedding | **~2.667 rps** por canal |
| WAF por `x-api-key` | 2 × a taxa do plano, em 5 min | 5.333 rps = **1,6 M em 5 min** |
| WAF por IP | Acima da soma dos tetos dos canais que saem pelo mesmo egress | ≥ 1,6 M em 5 min com um canal por IP. O valor de hoje (300.000, 1.000 rps) barraria um canal desses saindo por um IP só |
| Quota diária | 2 × o maior dia do canal no histórico | O enunciado não dá volume diário, só o pico. Teto físico do plano: 2.667 × 86.400 ≈ 230 M/dia; uma quota nesse valor não governa nada |
| Soma dos planos | ≤ throttle da conta | 8.000 rps, 80% dos 10.000 |

Com canais desiguais a conta é a mesma: o autorizador de cartões leva a maior fatia, e o plano dele sobe pela mesma regra. A soma dos tetos do WAF (16.000 rps) passa do throttle da conta de propósito: o WAF corta um canal descontrolado, e o total é limitado pelo throttle da conta e, atrás dele, pelo load shedding.

**O throttle da conta não tem folga para uma segunda API.** Os 10.000 RPS por conta e região somam todas as APIs da conta. O pico usa ~50% (55% com as ~500 recusas/s da seção 1.1); com a folga de 1,6×, 80–88%. Outra API de porte parecido na mesma conta não cabe, e por isso a conta é dedicada por domínio (ADR-0010). Passar disso pede aumento do throttle de conta, que é quota ajustável e entra no runbook R3 com as outras.

### 1.5 Réplica do legado: carga inicial e acesso por conta

**A carga inicial não cabe no feed.** O `limites-acl` é 1 task de 0,25 vCPU (`environments/sandbox/main.tf`, `max_tasks = 1`) que processa a fila em série: por registro, um `GetItem` consistente, a escrita condicional e o `DeleteMessage`. Com os ~4,6 ms de CPU por operação medidos no `limites` (seção 3; o consumidor em si não foi medido), 0,25 vCPU dá ~54 registros/s: **240–400 M contratos levariam ~51 a ~85 dias**. Mais CPU não resolve sozinha, porque são 3 chamadas de rede em série por registro. E o alarme `defasagem-do-legado` (300 s) ficaria disparado a carga inteira.

**Recomendação: carga inicial por DynamoDB import from S3; o feed só com os deltas.** O arquivo do legado passa em lote pelo mesmo tradutor da ACL (`traducao.py`) e vira DynamoDB JSON num bucket. O import cria a tabela `limites` já carregada (ele só cria tabela nova) e não consome capacidade de escrita. O que chegar pelo feed durante o import espera na fila (retenção de 14 dias) e entra depois: registro com versão menor ou igual à do arquivo vira OBSOLETA (ADR-0014), então a sobreposição é segura. Para os deltas, a task de hoje aguenta ~54 registros/s (~4,7 M/dia); acima disso, mais tasks, o que é seguro porque o compare-and-set é por versão.

**Sem acesso por conta.** A tabela `limites` tem só `id_contrato` como chave e nenhum GSI por `id_conta` (`modules/dados/main.tf`): listar os 3–5 contratos de uma conta hoje é Scan. O enunciado não pede essa consulta, mas uma plataforma de 80 M de contas vai precisar dela. O caminho é um GSI por `id_conta` com projeção `KEYS_ONLY`: reserva e débito não mudam `id_conta`, então o índice não é escrito no caminho quente.

### 1.6 Escritas no DynamoDB por operação e por mix

Na AWS foi medido só o DEBITO (a carga do k6 da seção 3, consumo por tabela). Os outros tipos são **derivados**: a sequência de escritas de cada um, lida no código, com os valores medidos onde o item é o mesmo e as regras de cobrança da AWS. Escrita de item até 1 KB custa 1 WRU; escrita transacional, 2 WRU por KB; `DeleteItem` e `BatchWriteItem` cobram pelo tamanho do item apagado; escrita condicional que falha também consome; e cada escrita num GSI é cobrada à parte, no índice. Os tamanhos são estimados a partir do código (`_para_item` e `_item_do_outbox` em `autorizacao/adapters/outbound/dynamodb.py`) com as regras de tamanho de item da AWS: a autorização tem menos de 1 KB (483 a 760 bytes); o item do outbox leva o CloudEvent inteiro e tem de 1.064 a 1.260 bytes, conforme o tipo de evento e a presença de `traceparent`. Na cobrança, 2 KB. Daí os 6,12 medidos no outbox: 4 do `Put` na transação e 2 do delete do relay.

| Operação (por chamada) | `limites` | `autorizacoes` | `autorizacoes-outbox` | Contabilização (inbox + lançamento) | Tabelas | GSIs |
|---|---|---|---|---|---|---|
| DEBITO (**medido**); CREDITO e RESERVA aprovados fazem as mesmas escritas | 1,09 | 3,06: `PutItem` condicional (1) + `Put` na transação (2) | 6,12 | 4,28 | **14,6** | 2 no `gsi_orfas` (entra ao registrar, sai ao concluir); a RESERVA soma 1 no `gsi_vencimentos` |
| Captura | ~1,1 | ~5,1: mais o `Update` da reserva original na mesma transação (2) | ~12,2: 2 eventos | ~8,6: 2 eventos | **~27,0** | 3 (a original sai do `gsi_vencimentos`) |
| Recusa | ~1,1 (a escrita condicional que falha é cobrada) | 3,06 | 6,12 | 0 (`TransacaoRecusada` não vai para a contabilização) | ~10,3 | 2 |
| Estorno | ~1,1 | ~6,1: mais a marca `estorno_em_curso` na original (1) e o `Update` da original na transação (2) | 6,12 | 4,28 | ~17,6 | 2; 4 se a original é RESERVA |
| Expiração de reserva (assíncrona) | ~1,1 | 2: `Update` da reserva na transação | 6,12 | 4,28 | ~13,5 | 1 |

Os 3,06 medidos no `autorizacoes` batem com a conta da tabela sozinha (1 + 2): as escritas nos GSIs não cabem nesse número e entram à parte, derivadas, a ~1 WRU cada (entrada menor que 1 KB com projeção `ALL`).

No pico de 5.000 aprovações/s + ~500 recusas/s, com os mixes da seção 1.1, em WRU/s:

| Mix | `autorizacoes-outbox` | `autorizacoes` | Cada tabela da contabilização | `limites` | GSIs | Total |
|---|---|---|---|---|---|---|
| Só DEBITO | ~34 k | ~17 k | ~11 k | ~6 k | ~11 k | ~89 k |
| Cartão: metade reserva, metade captura | **~49 k** | ~22 k | ~16 k | ~6 k | ~16 k | ~125 k |
| Pior caso: toda aprovação é captura | **~64 k** | ~27 k | ~21 k | ~6 k | ~16 k | ~156 k |

**O outbox estoura a quota padrão.** Em on-demand, a quota é de 40.000 WRU/s por tabela, ajustável, e vale também para cada GSI isoladamente. Com uma fração *f* das aprovações sendo captura, o outbox recebe 33.660 + 30.600·*f* WRU/s e passa de 40.000 a partir de *f* ≈ 21%. As outras tabelas e os GSIs ficam abaixo de 40 k nos três mixes; o maior é o `autorizacoes`, ~27 k no pior caso. **Pedir ~75.000 WRU/s por tabela** (pior caso + ~15%, a mesma folga da seção 1.1). O warm throughput do ADR-0013 foi dimensionado com o DEBITO (61.000 no outbox = 2 × 5.000 × 6,12); a mesma regra de 2× o pico dá ~98 k no mix de cartão.

**Fora desta tabela: as inboxes dos consumidores de referência** (antifraude e notificações, ADR-0009). Cada evento novo custa 2 WRU na inbox do consumidor (reivindicar e concluir, `consumidores/inbox_dynamodb.py`) e cada duplicata, 1: com ~5.500 eventos/s em cada fila, ~22 k WRU/s (derivado do código), +14% a +25% sobre os totais acima.

Fontes: consumo por operação, https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html; escrita em GSI, https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html; quotas, https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html.

## 2. Medição local (LocalStack) — o que ela prova e o que não prova

**Prova (corretude):** a corrida de 100 reservas de R$ 100, disparadas por 50 clientes em paralelo num limite de R$ 1.000, termina com 10 × 201, 90 × 402 e disponível final 0, pela API HTTP ponta a ponta (`make demo`).

**Não prova (desempenho).** O DynamoDB da LocalStack 4.14 foi medido isoladamente nesta máquina, sem o nosso código:

| Threads | UpdateItem/s | ms/op (parede) |
|---|---|---|
| 1 | 49 | 20,3 |
| 20 | **67** | 14,8 |

Ele praticamente **não escala com concorrência**. Com ~5 operações de banco por autorização, o teto local é ~13 autorizações/s, que é o que o k6 local observou (~15 rps, p50 ~1,9 s por fila). A latência **sequencial** local é ~99 ms por autorização (4 escritas e 1 salto HTTP no emulador).

**Conclusão:** a latência local reflete o emulador. O número de desempenho vem da AWS (seção 3).

## 3. Medição na AWS (sandbox sa-east-1, 20/10/2026; o teste de 5.000 rps de 21/10 está na seção 3.1)

**Como:** k6 `constant-arrival-rate` disparado da máquina do operador (Hortolândia/SP) contra o API Gateway em sa-east-1, carga de `DEBITO` R$ 1,00 espalhada em **5.000 contratos** criados pela ACL (`CTR-K-000001..005000`), como é a realidade de 80 M de contas. A latência do k6 inclui a internet até a região; a do gateway (`Latency`/`IntegrationLatency`) não. Tasks Fargate x86 de **1 vCPU / 2 GB**, 4 de cada serviço (o autoscaling subiu o `autorizacao` para 6 no último degrau). O WAF por IP foi elevado para a medição (`waf_limite_por_ip=20000000`; o default do sandbox hoje é 300.000 por IP em 5 min, 1.000 rps; na medição era 2.000), porque o k6 sai de um IP só. Métricas: Container Insights, API Gateway, DynamoDB.

**Medição fora da tabela:** `tests/load/resultados/vazao-2026-10-20T13-07-54-882Z.json` (13:07 UTC, 10:07 em Brasília) é anterior a todos os degraus abaixo: ~200 rps por 60 s (11.558 requisições feitas + 443 descartadas pelo k6), 168 rps efetivos, **64,5%** fora de 201/402 e p99 de 10,8 s. Pelos registros do deploy daquela manhã (não versionados), a versão no ar era a **1.0.0**: o apply das 10:01 usou a tag 1.0.0 e o plano da 1.0.1 é das 10:38. Ou seja, antes do keep-alive e da redução de CPU da 1.0.3. **O JSON guarda o status de cada resposta** (os contadores `st_*` de `tests/load/autorizacao.js`): 4.108 × 201, **7.435 × 503** e 15 × 500, que somam as 11.558. Não sobra espaço para 403 nem 429: o WAF e o throttle do gateway não barraram nada, e as falhas vieram do serviço. O 503 é a resposta de sobrecarga da `autorizacao` (load shedding, disjuntor aberto, timeout ou 5xx do salto para o `limites`; o contador não separa os motivos), e as aceitas esperaram 8,5 s no p50, fila de segundos. Os 15 × 500 são consistentes com o erro de keep-alive descrito abaixo, que a 1.0.0 ainda tinha. Fica no repositório como registro de uma versão anterior, não como medição de capacidade.

**Corretude no DynamoDB real:** 100 reservas paralelas de R$ 100 num limite de R$ 1.000 → `{201: 10, 402: 90}`, disponível 0, versão 11 (`tests/e2e/aws.py corrida`; repetido 4 vezes ao longo do dia).

| Degrau | Versão | Resultado (k6) | Gateway p50 / p95 / p99 | CPU média |
|---|---|---|---|---|
| 300 rps, 60 s | 1.0.2 | **243 rps**, 15.315 × 201, 481 × 503, e 2.205 iterações descartadas pelo k6 (12% das 18.000 pretendidas); p50 2,8 s | 805 / — / 5.763 ms | autorizacao **97%** (saturado), limites 40% |
| 150 rps, 90 s | 1.0.2 | 148,9 rps, 13.500 × 201/402 e **1 × 500** (o do keep-alive, ver abaixo); p50 85, p95 251, **p99 628 ms** | — | — |
| 150 rps, 90 s | 1.0.3 | 148,7 rps, **100% 201**; p50 71, p95 120, p99 **199 ms** | 54 / 90 / 164 ms | autorizacao 33%, limites 21% |
| 300 rps, 90 s | 1.0.3 | 299 rps, **100% 201**; p50 79, p95 284, p99 537 ms | 58 / 231 / 526 ms | autorizacao 60% |
| 600 rps, 180 s | 1.0.3, `MAX_EM_VOO=256` | 377 rps de 201 + 37.476 × 503 (shedding), e 1.399 iterações descartadas pelo k6; p50 das aceitas **2,7 s** (o de todas, 1,8 s, mistura os 503, recusados na chegada sem entrar na fila) | — | autorizacao 100% |
| 600 rps, 120 s | 1.0.3, `MAX_EM_VOO=32`, 6 tasks | 568 rps de 201 + 3.188 × 503 (4,4%); p50 81 ms, p99 972 ms | 59 / 519 / 874 ms | autorizacao 78%, limites 66% |

**Versão medida × versão entregue.** Todos os degraus são da imagem **1.0.3** (20/10). As correções de 21/10 (painel de revisão) mexeram no caminho quente e a latência **não foi remedida** (custo e janela do sandbox). O que entrou por requisição: validação por regex do `traceparent` e do `X-Client-Id` na `autorizacao` e mais uma condição (`id_conta`) na mesma `ConditionExpression` do `UpdateItem` no `limites`. Nenhuma chamada de rede nova. O smoke da esteira no código atual repetiu a corrida `{201: 10, 402: 90}` e os 10 lançamentos contábeis, que provam corretude, não latência. O teste de 5.000 rps da seção 3.1 roda a imagem com essas correções (commit `7531b6e`), mas mede vazão com a frota no limite de CPU, não latência com folga: não substitui os degraus abaixo como referência de p99.

**O que mudou entre 1.0.2 e 1.0.3** (a CPU por autorização caiu de ~16 ms para ~8 ms):
- `--no-access-log` (o gateway já registra cada requisição);
- `--timeout-keep-alive 400`: com o default de 5 s, o gateway reaproveitava conexão em fechamento e devolvia 500 (`Execution failed due to a network error communicating with endpoint: Connection is closed`, no log de execução do gateway). Zero 500 depois da troca;
- pedido sem original grava primeiro (`PutItem` condicional devolve a gravada se já existir): um `GetItem` a menos no caminho quente.

**Capacidade por vCPU (x86, 1.0.3):**

| Serviço | CPU por operação | Vazão a 100% | Vazão a 60% (folga para pico) | Vazão a ~33% (p99 de gateway ≤ ~165 ms) |
|---|---|---|---|---|
| autorizacao | ~8 ms | ~122 rps | ~73 rps | ~38 rps |
| limites | ~4,6 ms | ~218 op/s | ~130 op/s | ~70 op/s |

**Latência por salto** (minuto de 600 rps): `UpdateItem` no limite ~4 ms; `PutItem` ~3,6 ms; **`TransactWriteItems` (autorização + outbox) ~21 ms**, o maior custo fixo do caminho síncrono. O resto do p50 é Python, gateway e VPC Link.

**Load shedding e autoscaling:**
- `MAX_EM_VOO` alto (256) só troca 503 rápido por fila lenta: com 256 em voo e ~95 rps por task, a espera chegava a segundos. A Lei de Little fecha com o JSON do degrau (`vazao-2026-10-20T14-19-42-809Z.json`): 376,6 rps de 201 × 2,64 s de latência média das aceitas ≈ **994 em voo** (visto do k6, que soma a ida e volta pela internet), encostado no portão de 4 tasks × 256 = 1.024. Se as aceitas levassem 1,8 s (o p50 de todas), seriam ~680 em voo, longe do portão, e os 35% de 503 ficariam sem explicação. As iterações descartadas (`dropped_iterations`) neste degrau e no de 300 rps da 1.0.2 (1.399 e 2.205) são o k6 sem VU livre para disparar no ritmo, porque as respostas demoravam segundos: nos dois, o rótulo de rps é o pretendido, não o disparado. A regra é capacidade da task × p99 alvo × 2. Com ~8 ms de CPU por autorização (1 / 8 ms = 125 rps por vCPU a 100% de CPU; o medido, acima, foi ~122): **18** na task entregue, de 0,5 vCPU (62,5 × 0,15 × 2), e ~37 com 1 vCPU. O degrau de 600 rps acima usou 32 com tasks de 1 vCPU. O Terraform agora deriva o valor da CPU da task (`local.max_em_voo`); antes ele era fixo em 32 e deixava passar o dobro da fila na task de 0,5 vCPU.
- O target tracking de CPU (60%, 3 pontos de 1 min) pediu a 5ª e a 6ª task **~6 min depois do início** da sobrecarga. Reativo não segura pico súbito. Para o pico previsível há janelas agendadas na IaC (`var.janelas_de_pico`; no sandbox, dia 1 de cada mês das 06:30 às 12:00 de Brasília com mínimo de 4 tasks), predictive scaling em `ForecastOnly` em `autorizacao` e `limites` (só previsão, sem agir) e warm throughput configurável no DynamoDB, desligado no sandbox por padrão e ligado uma vez, na janela da seção 3.1 (ADR-0013). O shedding protege o que já está no ar enquanto isso.

**Extrapolação para 5.000 TPS** (pico sustentado; ARM64 não foi medido, a conta é x86). Feita antes do teste de 21/10; a seção 3.1 diz o que o teste confirmou e o que mudou:

| Alvo | autorizacao | limites | Total |
|---|---|---|---|
| CPU a 60% (vazão; p99 de centenas de ms) | ~68 vCPU | ~38 vCPU | ~106 vCPU |
| CPU a ~33% (p99 de gateway ≤ ~165 ms, perto do RNF de 150 ms) | ~130 vCPU | ~72 vCPU | ~200 vCPU |

**Teto da stack entregue e quota:** com `tasks_max = 20` tasks de 0,5 vCPU, o `autorizacao` satura em ~730 rps a 60% de CPU (20 × 0,5 × 73). A 60% de CPU o alvo cabe na quota de Fargate da conta (106 de **140 vCPU**, `docs/operacao/infraestrutura.md`, seção 2), mas exige subir `tasks_max` (~136 tasks de 0,5 vCPU ou ~68 de 1 vCPU no `autorizacao`). Perto do RNF de latência (~33%) são ~200 vCPU: pedir o aumento da quota antes. O teste da seção 3.1 usou 60 tasks de 1 vCPU por serviço (120 vCPU), aceitou ~96% de 5.000 rps com a CPU a ~85% e aponta ~150–160 vCPU para aceitar tudo com folga: acima da quota de 140.

O RNF de p99 ≤ 150 ms **não foi atingido** em nenhum degrau, nem no teste de 5.000 rps (p99 de gateway de ~1,1–1,2 s no patamar, com a CPU a ~85%); o mais perto foi 164 ms no gateway a 33% de CPU. A distância é Python mais a transação de ~21 ms. Fechar isso pede a alavanca 1 da seção 5 (tira a transação do caminho comum) e, se ainda faltar, um runtime compilado nos dois serviços quentes (ADR-0003).

### 3.1 Teste de 5.000 rps na AWS (21/10/2026, 23:06–23:18 UTC)

**Configuração da janela** (apply local de medição; depois, apply de volta ao padrão: 2 tasks de 0,5 vCPU, plano e WAF estacionados):
- `autorizacao` e `limites` com **60 tasks Fargate x86 de 1 vCPU / 2 GB cada**, `tasks_min = tasks_max = 60` (autoscaling travado: mede a frota, não o scale-out). `MAX_EM_VOO` derivado da CPU: 37 por task. Imagem do commit `7531b6e` (`git-7531b6e80ff6`), com as correções de 21/10.
- A stack medida é a de antes dos consumidores de referência, da data platform, do schema registry e da política preditiva (integrados depois e ainda não implantados). No bus, o DEBITO casa 3 regras: contabilização, antifraude e notificações.
- Borda: `plano-carga` com 6.000 rps e burst de 5.000; WAF com 2.000.000 por `x-api-key` e 20.000.000 por IP em 5 min (`terraform.tfvars.example`).
- **Warm throughput** (pré-aquecimento, cobrado uma vez e irreversível): `autorizacoes-outbox` 32.000 de escrita, `autorizacoes` 17.000 e o `gsi_orfas` 11.000, `limites` 10.000, `contabilizacao-lancamentos` e `contabilizacao-inbox` 11.500 cada. O apply de volta ao padrão não mexeu nas tabelas, e a AWS não reduz warm throughput: esses valores ficam nelas até o destroy.
- **100.000 contratos de carga** `CTR-K-000001..100000`, criados direto no DynamoDB pelo caso de uso da ACL (`SincronizarComLegado`), com um script fora do repositório, na conta `0001-77777777-7` (a que o k6 manda para esse prefixo). No pico, 0,05 op/s por contrato: longe do teto de ~8 op/s do item (ADR-0016).
- **Gerador:** k6 v0.55.0 numa task Fargate de 8 vCPU / 16 GB **dentro de sa-east-1** (VPC do sandbox, IP público, security group só com saída 443), criada e apagada fora do Terraform. Cenário `pico` de `tests/load/autorizacao.js` (`ramping-arrival-rate`): 500 → 5.000 rps em 3 min e patamar de 8 min em 5.000, DEBITO de R$ 1,00 num contrato sorteado entre os 100.000. Parâmetros: `CENARIO=pico RPS=5000 RAMPA=3m DURACAO=8m N_CONTRATOS=100000 PREFIXO=CTR-K- PAD=6 VUS_PRE=2000 VUS_MAX=8000 P99_MS=1000`. Dentro da região, a latência do k6 não inclui a internet até São Paulo (a dos degraus de 20/10 incluía), e a vazão oferecida não depende da máquina nem da conexão do operador. Stack e gerador somaram 128,5 vCPU (60 + 60 + 0,25 + 0,25 + 8), dentro da quota de 140.

**k6** (`tests/load/resultados/pico-2026-10-21T23-18-14Z.json`, visto de dentro da região): 2.894.590 requisições em 661 s, 4.378,5 rps de média no teste inteiro (rampa incluída). Status: **2.784.648 × 201 (96,2%) e 109.941 × 503 (3,8%)**; nenhum 500, 402 ou 429. 316 iterações (0,01%) não foram disparadas por falta de VU livre. Latência de todas as respostas: p50 111,8 ms, p95 643,2 ms, p99 1.116,1 ms, máximo 9.566,9 ms; só das 201: p50 120,8, p95 648,9 e p99 1.102,8 ms. Os thresholds do cenário (p99 < 1.000 ms com `P99_MS=1000`, falhas < 1%) foram cruzados e o k6 saiu com 99: é o resultado honesto, não defeito do teste.

**CloudWatch, por minuto** (UTC; Brasília = UTC − 3). Latência = `Latency` do gateway; CPU = média do service; WCU/s = `ConsumedWriteCapacityUnits` da tabela, sem os GSIs. Os minutos 23:06 e 23:17 são parciais (o teste durou 661 s) e ficam fora.

| min (UTC) | req/s no gateway | p50 / p95 / p99 gateway (ms) | 5xx/s | CPU autorizacao / limites | WCU/s outbox | WCU/s autorizacoes | WCU/s limites | WCU/s lançamentos = inbox |
|---|---|---|---|---|---|---|---|---|
| 23:07 | 1.398 | 52 / 71 / 120 | 0 | 25 / 23% | 9.724 | 4.855 | 1.611 | 3.346 |
| 23:08 | 2.897 | 55 / 99 / 203 | 0 | 47 / 46% | 18.715 | 9.351 | 3.110 | 6.223 |
| 23:09 | 4.387 | 63 / 231 / 421 | 0 | 70 / 69% | 27.406 | 13.706 | 4.565 | 9.124 |
| 23:10 | 4.994 | 88 / 566 / 2.454 | 97 | 81 / 80% | 29.468 | 14.704 (**15.497 `WriteThrottleEvents`** no minuto) | 4.910 | 9.794 |
| 23:11 | 4.996 | 108 / 469 / 776 | 22 | 83 / 81% | 29.743 | 14.891 | 5.015 | 9.923 |
| 23:12 | 4.995 | 165 / 703 / 1.147 | 270 | 84 / 79% | 28.214 | 14.120 | 5.051 | 8.686 |
| 23:13 | 4.998 | 192 / 764 / 1.201 | 462 | 82 / 77% | 27.041 | 13.524 | 5.534 | 9.700 |
| 23:14 | 5.000 | 184 / 755 / 1.209 | 346 | 85 / 78% | 27.986 | 13.998 | 6.741 | 9.336 |
| 23:15 | 5.000 | 206 / 759 / 1.219 | 278 | 86 / 80% | 28.474 | 14.235 | 7.763 | 9.520 |
| 23:16 | 5.001 | 154 / 661 / 1.108 | 142 | 85 / 82% | 29.202 | 14.598 | 8.575 | 9.741 |

**Cadeia assíncrona sem perda** (CloudWatch, janela 23:05–23:40 UTC): as regras `para-contabilizacao`, `para-antifraude` e `para-notificacoes` tiveram 2.784.648 invocations cada, 0 falhas e 0 estranguladas; a fila `contabilizacao` recebeu 2.784.648 mensagens e apagou 2.784.648; erros das Lambdas do relay e da contabilização: 0; DLQs (`contabilizacao-dlq`, `relay-dlq`, `financas-entrega-dlq`): 0. Ou seja, as 2.784.648 autorizações aprovadas viraram 2.784.648 lançamentos processados. O relay (Stream do outbox) ficou com `IteratorAge` entre 1,3 e 1,7 s o tempo todo.

**Gargalos:**
- **CPU do caminho síncrono.** No patamar, `autorizacao` a 81–86% e `limites` a 77–82%. O load shedding devolveu o excesso como 503 rápido em vez de fila: entre 0,4% e 9,2% de 5xx por minuto no patamar (derivado: 5xx/s ÷ req/s), 3,8% no teste inteiro, nenhum 500. O contador não separa shedding, disjuntor e estrangulamento do DynamoDB; fora do minuto 23:10 não houve estrangulamento, e com a CPU a ~85% a leitura é shedding. O p99 de gateway de ~1,1–1,2 s de 23:12 em diante é o efeito da CPU alta e do shedding; o SLO de 150 ms (premissa nossa, ADR-0001; o enunciado pede ~5.000 TPS, não latência) continua não atingido.
- **Consumidor contábil.** Com o teto de 200 no ESM, ele ficou para trás: a mensagem mais antiga da fila `contabilizacao` chegou a 51, 113, 173, 208, 277 e 335 s (23:13 a 23:18), acima do limiar de 300 s do alarme `idade-da-fila-contabilizacao` (R13) no último minuto, e a fila **drenou a zero depois do teste**, sem erro e sem DLQ. A estimativa da seção 1.1 não se confirmou. Para acompanhar 5.000 eventos/s em tempo real, ele precisa de mais concorrência ou de lote maior, medidos antes do pico.
- **DynamoDB.** Com o warm throughput, o único estrangulamento foi em `autorizacoes` no minuto da chegada ao patamar (23:10), que coincide com o p99 de 2.454 ms daquele minuto; zero no resto, em todas as tabelas. Esta coleta não separa a tabela do `gsi_orfas` como causa. Escrita nas 5 tabelas no patamar: ~64,8–71,9 k WRU/s, ~13,7–14,8 WRU por autorização aceita (derivado; a contabilização entra com o atraso do consumidor), em linha com os 14,6 medidos em 20/10. A escrita do `limites` por operação subiu ao longo do patamar, de ~1,0 para ~1,8 WRU (derivado); a hipótese é a guarda de 5 min crescendo no item, não verificada item a item.

**CPU por requisição e o que falta para aceitar 100%** (derivado: CPU média × 60 vCPU ÷ req/s, de 23:07 a 23:11): ~10 ms por requisição no `autorizacao` (9,6 a 10,7 ms por minuto; contra ~8 ms em 20/10) e ~9,5 ms por operação no `limites` (9,4 a 9,9 ms; contra ~4,6 ms). A diferença não foi decomposta: mudaram a imagem (correções de 21/10) e a carga (100.000 contratos, 60 tasks). A ~10 ms, 5.000 rps ocupam ~50 vCPU por serviço; para aceitar tudo com folga são **~75 a 80 tasks de 1 vCPU por serviço** (CPU a ~62–67%), **~150–160 vCPU contra a quota de 140**. Fica dentro da faixa extrapolada acima (~106–200 vCPU), mais perto do teto, e confirma o pedido de quota de Fargate do runbook R3.

**O que muda na leitura da extrapolação:**
- 5.000 TPS deixou de ser só conta: o caminho síncrono recebeu 5.000 rps oferecidos por 8 min e aceitou ~96%, e a cadeia assíncrona levou cada aprovação até o lançamento, sem perda.
- O gargalo previsto, CPU do síncrono, apareceu onde a conta dizia, e a falta de capacidade se comportou como o desenho manda: 503 rápido, nenhum 500 nem timeout do gateway.
- A CPU por requisição a 5.000 rps foi maior que a dos degraus de 20/10: a tabela "Capacidade por vCPU" acima é otimista para este volume.
- Apareceu um gargalo que a extrapolação não pegava: o consumidor contábil com teto de 200.
- Os degraus de 150 e 300 rps continuam sendo a referência de p99 com folga de CPU; este teste mede vazão com a frota no limite.

**Custo da janela (estimativa; o Cost Explorer mostra o valor com atraso e não foi conferido):** pré-aquecimento ~US$ 67 (derivado: ~69.000 unidades de escrita acima das 4.000 que cada tabela e GSI on-demand já tem, × US$ 0,000975 por unidade em sa-east-1, Pricing API); escrita ~74 M WRU na janela, ≈ US$ 70; API Gateway REST com ~2,9 M requisições; Fargate com ~121 vCPU da stack por ~50 min, mais o gerador. Total na faixa de **US$ 150–180**; não afirmo valor exato.

## 4. Custo (ordem de grandeza, sandbox)

| Item | Observação |
|---|---|
| Fargate x86 (sandbox) | `autorizacao` e `limites`: 2 tasks × 0,5 vCPU / 1 GB cada; `limites-acl` e `reconciliador`: 1 task × 0,25 vCPU / 0,5 GB. ~US$ 5,1/dia, o maior custo ocioso |
| IPv4 público | Um por task (6) × US$ 0,005/h, ~US$ 0,7/dia |
| NLB + VPC Link | Custo fixo por hora (NLB: US$ 0,034/h) |
| WAF | US$ 5/mês pelo web ACL + US$ 1/mês por regra (3) |
| DynamoDB, SQS, EventBridge, Lambda | On-demand, centavos em volume de demo. Inclui as inboxes e as funções dos consumidores de referência |
| Data platform e schema registry | Firehose Direct PUT e S3 por uso, sem custo por hora parado; o registry não é cobrado com o schema discovery desligado; o alarme de defasagem entra no custo dos alarmes (`docs/operacao/infraestrutura.md`, seção 7) |
| NAT Gateway | **Não usado** (~US$ 68/mês parado em sa-east-1, valor reportado pelo painel de revisão) |
| Proteção | Budget de US$ 30/mês com alertas; janela curta com destroy |

Fixo com o sandbox no ar: ~US$ 7/dia. Conta item a item e o que não foi quantificado: `docs/operacao/infraestrutura.md`, seção 7.

Preços **oficiais de sa-east-1** consultados na AWS Pricing API em 20/10/2026: Fargate x86 US$ 0,0696 por vCPU-hora e US$ 0,0076 por GB-hora; Fargate ARM US$ 0,0557 e US$ 0,00612; DynamoDB on-demand US$ 0,9375 por milhão de WRU e US$ 0,1875 por milhão de RRU.

**Custo por hora no pico de 5.000 TPS** (só os dois itens dominantes):

| Item | Conta | US$/hora |
|---|---|---|
| Fargate x86, CPU a 60% | 106 vCPU × 0,0696 + 212 GB × 0,0076 | ~9,0 |
| Fargate x86, CPU a ~33% | 200 vCPU × 0,0696 + 400 GB × 0,0076 | ~17,0 |
| DynamoDB on-demand, escrita (tabelas + GSIs, seção 1.6) | 89 k a 156 k WRU/s, conforme o mix, × 3.600 s × 0,9375 / 10⁶ | **~300 a ~526** |
| DynamoDB on-demand, inboxes dos consumidores de referência (seção 1.6) | ~22 k WRU/s × 3.600 s × 0,9375 / 10⁶ | ~74 |

**A conta de banco domina, de longe**, e não a de computação. O teste de 5.000 rps (seção 3.1) confere a ordem de grandeza: ~64,8–71,9 k WRU/s nas 5 tabelas no patamar, sem GSIs e sem recusas, ≈ US$ 219–243/h (derivado). Só em escrita no DynamoDB, com os GSIs: ~US$ 15,5 por milhão de DEBITO (16,6 WRU cada) e ~US$ 28 por milhão de capturas (~30 WRU). A versão anterior desta tabela dava US$ 246/h e US$ 13,7 por milhão porque só contava o DEBITO, sem recusas e sem os GSIs. Medidos são só os 14,6 WRU de tabela do DEBITO; o resto é derivado (seção 1.6).

## 5. Alavancas de custo e latência, em ordem de retorno

1. **Outbox dentro do item da autorização**, publicado pelo Stream da própria tabela `autorizacoes`, em vez de uma tabela de outbox com transação. Some o `autorizacoes-outbox` (6,1 WRU por evento, 42% das escritas de tabela do DEBITO; 12,2 na captura) e, com ele, a tabela que estoura a quota nos mixes com captura (seção 1.6). A transação vira um `PutItem` condicional e sai do caminho síncrono a operação mais lenta (~21 ms → ~4 ms). O item da autorização passa a levar o evento e, no DEBITO, fica com 1–2 KB: a escrita dele continua em ~2 WRU, e a economia é a do outbox. A conta de quota do `autorizacoes` precisa ser refeita com o item maior. Estorno e captura continuam com transação (dois itens). Custo: o relay passa a filtrar o Stream da tabela quente pelos itens com evento novo.
2. **Capacidade provisionada com auto scaling, e reserva para a base**, no lugar de on-demand para o tráfego estável; o on-demand fica para o imprevisível. A cotação depende do perfil de tráfego (não medido aqui).
3. **Contabilização**: inbox + lançamento custam 4,3 WRU por evento contabilizado (a captura contabiliza 2). O lançamento já tem chave determinística (`Lancamento.id_para(source, id)`); avaliar se a escrita condicional dele basta como deduplicação e dispensa o inbox separado.
4. **Graviton (ARM64)**: 20% mais barato por vCPU-hora; a vazão por vCPU precisa ser medida antes de contar com ela.
5. **Runtime compilado** nos serviços quentes: corta a computação (que já é a menor parte) e ajuda o p99.
