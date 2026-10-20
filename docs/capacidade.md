# Capacidade, desempenho e custo

## 1. Dimensionamento (de onde vêm os números)

| Grandeza | Conta | Valor |
|---|---|---|
| Itens de Limite | 80 M contas × 3–5 contratos | 240–400 M itens (~100–160 GB) |
| Requisições em voo no pico | Lei de Little: 5.000 TPS × ~50 ms | ~250 |
| Escritas por autorização | **Medido na AWS** (seção 3): `limites` 1,09 + `autorizacoes` 3,06 + `autorizacoes-outbox` 6,12 + contabilização 2,14 + 2,14 | **~14,6 WRU** (a estimativa inicial, ~6–8, esquecia o outbox e a contabilização) |
| WRU no pico | 5.000 × 14,6 | ~73 k WRU/s somando as 5 tabelas; a maior é o outbox, ~31 k/s (abaixo dos 40 k por tabela da quota padrão) |
| Eventos no pico | Depende do mix: captura publica **2** eventos (seção 1.1) | 5,5 k a **10,5 k** entradas de PutEvents/s (quota aprovada: **6.000**; estoura com mais de 10% de capturas) |
| Invocations no pico | Regras casadas por evento (seção 1.1) | 16 k a **21 k**/s (quota aprovada: **20.000**; estoura com mais de 80% de capturas) |
| Teto por contrato | 1.000 WCU/s por partição, com a guarda de 5 min no item (o custo por escrita cresce com a taxa) | ~8 op/s sustentadas (conta no ADR-0016) |
| Teto por chave dos GSIs esparsos | 2 escritas no `gsi_orfas` por autorização, divididas por N shards; sort key monotônica (seção 1.2) | N = 10: **~1.000 WCU/s por chave, 100% do teto**. N = 64 (padrão agora): ~156 |
| Item do limite com reservas | Holds de até 7 dias moram no item; ~45 bytes cada (seção 1.3) | ~9.000 reservas ativas = 400 KB (teto do item) |
| API Gateway | 10.000 RPS / burst 5.000 por conta e região | O pico usa ~50% |

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

No pico de 5.000 aprovações/s + ~500 recusas/s:

| Mix | Entradas de PutEvents/s | Invocations/s |
|---|---|---|
| Só DEBITO (a conta antiga) | 5.500 | 16.000 |
| Cartão: metade reserva, metade captura | 8.000 | 18.500 |
| **Pior caso: toda aprovação é captura** (reservas feitas antes do pico) | **10.500** | **21.000** |

**As quotas aprovadas não bastam.** Com uma fração *f* das aprovações sendo captura, são 5.500 + 5.000·*f* entradas/s e 16.000 + 5.000·*f* invocations/s: PutEvents 6.000 estoura a partir de *f* = 10%; Invocations 20.000 aguenta o mix de cartão usando 92% da quota e estoura a partir de *f* = 80%. **Pedir: PutEvents 12.000/s e Invocations 25.000/s** (pior caso + ~15–20%). A tabela de quotas da AWS fala em requisições de PutEvents por segundo e não diz se uma requisição de 10 entradas (o lote do relay) conta 1 ou 10: a conta acima usa a leitura conservadora (entradas). O fan-out por SNS cortaria as invocations (ADR-0009), com outros custos.

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

## 2. Medição local (LocalStack) — o que ela prova e o que não prova

**Prova (corretude):** a corrida de 100 reservas paralelas termina com 10 × 201 e 90 × 402, saldo final 0 e invariantes preservadas, pela API HTTP ponta a ponta (`make demo`).

**Não prova (desempenho).** O DynamoDB da LocalStack 4.14 foi medido isoladamente nesta máquina, sem o nosso código:

| Threads | UpdateItem/s | ms/op (parede) |
|---|---|---|
| 1 | 49 | 20,3 |
| 20 | **67** | 14,8 |

Ele praticamente **não escala com concorrência**. Com ~5 operações de banco por autorização, o teto local é ~13 autorizações/s, que é o que o k6 local observou (~15 rps, p50 ~1,9 s por fila). A latência **sequencial** local é ~99 ms por autorização (4 escritas e 1 salto HTTP no emulador).

**Conclusão:** a latência local reflete o emulador. O número de desempenho vem da AWS (seção 3).

## 3. Medição na AWS (sandbox sa-east-1, 20/10/2026)

**Como:** k6 `constant-arrival-rate` disparado da máquina do operador (Hortolândia/SP) contra o API Gateway em sa-east-1, carga de `DEBITO` R$ 1,00 espalhada em **5.000 contratos** criados pela ACL (`CTR-K-000001..005000`), como é a realidade de 80 M de contas. A latência do k6 inclui a internet até a região; a do gateway (`Latency`/`IntegrationLatency`) não. Tasks Fargate x86 de **1 vCPU / 2 GB**, 4 de cada serviço (o autoscaling subiu o `autorizacao` para 6 no último degrau). O WAF por IP foi elevado para a medição (`waf_limite_por_ip=20000000`; o default do sandbox é 2.000 por IP em 5 min), porque o k6 sai de um IP só. Métricas: Container Insights, API Gateway, DynamoDB.

**Medição fora da tabela:** `tests/load/resultados/vazao-2026-10-20T13-07-54-882Z.json` (13:07 UTC, 10:07 em Brasília) é anterior a todos os degraus abaixo: ~200 rps por 60 s (11.558 requisições feitas + 443 descartadas pelo k6), 168 rps efetivos, **64,5%** fora de 201/402 e p99 de 10,8 s. Pelos registros do deploy daquela manhã (não versionados), a versão no ar era a **1.0.0**: o apply das 10:01 usou a tag 1.0.0 e o plano da 1.0.1 é das 10:38. Ou seja, antes do keep-alive e da redução de CPU da 1.0.3. O JSON não guarda o status das falhas nem o limite do WAF naquele momento, então **a causa das falhas não é afirmada aqui**. Fica no repositório como registro, não como medição de capacidade.

**Corretude no DynamoDB real:** 100 reservas paralelas de R$ 100 num limite de R$ 1.000 → `{201: 10, 402: 90}`, disponível 0, versão 11 (`tests/e2e/aws.py corrida`; repetido 4 vezes ao longo do dia).

| Degrau | Versão | Resultado (k6) | Gateway p50 / p95 / p99 | CPU média |
|---|---|---|---|---|
| 300 rps, 60 s | 1.0.2 | **243 rps**, 15.315 × 201, 481 × 503; p50 2,8 s | 805 / — / 5.763 ms | autorizacao **97%** (saturado), limites 40% |
| 150 rps, 90 s | 1.0.2 | 148,9 rps, 13.500 × 201/402 e **1 × 500** (o do keep-alive, ver abaixo); p50 85, p95 251, **p99 628 ms** | — | — |
| 150 rps, 90 s | 1.0.3 | 148,7 rps, **100% 201**; p50 71, p95 120, p99 **199 ms** | 54 / 90 / 164 ms | autorizacao 33%, limites 21% |
| 300 rps, 90 s | 1.0.3 | 299 rps, **100% 201**; p50 79, p95 284, p99 537 ms | 58 / 231 / 526 ms | autorizacao 60% |
| 600 rps, 180 s | 1.0.3, `MAX_EM_VOO=256` | 374 rps de 201 + 37.476 × 503 (shedding); p50 das aceitas **1,8 s** | — | autorizacao 100% |
| 600 rps, 120 s | 1.0.3, `MAX_EM_VOO=32`, 6 tasks | 573 rps de 201 + 3.188 × 503 (4,4%); p50 81 ms, p99 972 ms | 59 / 519 / 874 ms | autorizacao 78%, limites 66% |

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
- `MAX_EM_VOO` alto (256) só troca 503 rápido por fila lenta: com 256 em voo e ~95 rps por task, a espera chegava a segundos. A regra é capacidade da task × p99 alvo × 2. Com ~8 ms de CPU por autorização (1 / 8 ms = 125 rps por vCPU a 100% de CPU; o medido, acima, foi ~122): **18** na task entregue, de 0,5 vCPU (62,5 × 0,15 × 2), e ~37 com 1 vCPU. O degrau de 600 rps acima usou 32 com tasks de 1 vCPU. O Terraform agora deriva o valor da CPU da task (`local.max_em_voo`); antes ele era fixo em 32 e deixava passar o dobro da fila na task de 0,5 vCPU.
- O target tracking de CPU (60%, 3 pontos de 1 min) pediu a 5ª e a 6ª task **~6 min depois do início** da sobrecarga. Reativo não segura pico súbito. Para o pico previsível há janelas agendadas na IaC (`var.janelas_de_pico`; no sandbox, dia 1 de cada mês das 06:30 às 12:00 de Brasília com mínimo de 4 tasks) e warm throughput configurável no DynamoDB, desligado no sandbox (ADR-0013). O shedding protege o que já está no ar enquanto isso.

**Extrapolação para 5.000 TPS** (pico sustentado; ARM64 não foi medido, a conta é x86):

| Alvo | autorizacao | limites | Total |
|---|---|---|---|
| CPU a 60% (vazão; p99 de centenas de ms) | ~68 vCPU | ~38 vCPU | ~106 vCPU |
| CPU a ~33% (p99 de gateway ≤ ~165 ms, perto do RNF de 150 ms) | ~130 vCPU | ~72 vCPU | ~200 vCPU |

**Teto da stack entregue e quota:** com `tasks_max = 20` tasks de 0,5 vCPU, o `autorizacao` satura em ~730 rps a 60% de CPU (20 × 0,5 × 73). A 60% de CPU o alvo cabe na quota de Fargate da conta (106 de **140 vCPU**, `docs/operacao/infraestrutura.md`, seção 2), mas exige subir `tasks_max` (~136 tasks de 0,5 vCPU ou ~68 de 1 vCPU no `autorizacao`). Perto do RNF de latência (~33%) são ~200 vCPU: pedir o aumento da quota antes.

O RNF de p99 ≤ 150 ms **não foi atingido** em nenhum degrau; o mais perto foi 164 ms no gateway a 33% de CPU. A distância é Python mais a transação de ~21 ms. Fechar isso pede a alavanca 1 da seção 5 (tira a transação do caminho comum) e, se ainda faltar, um runtime compilado nos dois serviços quentes (ADR-0003).

## 4. Custo (ordem de grandeza, sandbox)

| Item | Observação |
|---|---|
| Fargate x86 (sandbox) | `autorizacao` e `limites`: 2 tasks × 0,5 vCPU / 1 GB cada; `limites-acl` e `reconciliador`: 1 task × 0,25 vCPU / 0,5 GB. ~US$ 5,1/dia, o maior custo ocioso |
| IPv4 público | Um por task (6) × US$ 0,005/h, ~US$ 0,7/dia |
| NLB + VPC Link | Custo fixo por hora (NLB: US$ 0,034/h) |
| WAF | US$ 5/mês pelo web ACL + US$ 1/mês por regra (2) |
| DynamoDB, SQS, EventBridge, Lambda | On-demand, centavos em volume de demo |
| NAT Gateway | **Não usado** (~US$ 68/mês parado em sa-east-1, valor reportado pelo painel de revisão) |
| Proteção | Budget de US$ 30/mês com alertas; janela curta com destroy |

Fixo com o sandbox no ar: ~US$ 7/dia. Conta item a item e o que não foi quantificado: `docs/operacao/infraestrutura.md`, seção 7.

Preços **oficiais de sa-east-1** consultados na AWS Pricing API em 20/10/2026: Fargate x86 US$ 0,0696 por vCPU-hora e US$ 0,0076 por GB-hora; Fargate ARM US$ 0,0557 e US$ 0,00612; DynamoDB on-demand US$ 0,9375 por milhão de WRU e US$ 0,1875 por milhão de RRU.

**Custo por hora no pico de 5.000 TPS** (só os dois itens dominantes):

| Item | Conta | US$/hora |
|---|---|---|
| Fargate x86, CPU a 60% | 106 vCPU × 0,0696 + 212 GB × 0,0076 | ~9,0 |
| Fargate x86, CPU a ~33% | 200 vCPU × 0,0696 + 400 GB × 0,0076 | ~17,0 |
| DynamoDB on-demand, escrita | 5.000 × 14,6 WRU × 3.600 s × 0,9375 / 10⁶ | **~246** |

**A conta de banco domina, de longe**, e não a de computação. Por autorização: ~US$ 0,0000137 só em escrita (US$ 13,7 por milhão).

## 5. Alavancas de custo e latência, em ordem de retorno

1. **Outbox dentro do item da autorização**, publicado pelo Stream da própria tabela `autorizacoes`, em vez de uma tabela de outbox com transação. Some o `autorizacoes-outbox` (6,1 WRU, 42% do total), a transação vira um `PutItem` condicional (a autorização paga 1× em vez de 2×) e sai do caminho síncrono a operação mais lenta (~21 ms → ~4 ms). Estorno e captura continuam com transação (dois itens). Custo: o relay passa a filtrar o Stream da tabela quente pelos itens com evento novo.
2. **Capacidade provisionada com auto scaling, e reserva para a base**, no lugar de on-demand para o tráfego estável; o on-demand fica para o imprevisível. A cotação depende do perfil de tráfego (não medido aqui).
3. **Contabilização**: inbox + lançamento custam 4,3 WRU por autorização. O lançamento já tem chave determinística (`Lancamento.id_para(source, id)`); avaliar se a escrita condicional dele basta como deduplicação e dispensa o inbox separado.
4. **Graviton (ARM64)**: 20% mais barato por vCPU-hora; a vazão por vCPU precisa ser medida antes de contar com ela.
5. **Runtime compilado** nos serviços quentes: corta a computação (que já é a menor parte) e ajuda o p99.
