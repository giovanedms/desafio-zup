# Capacidade, desempenho e custo

## 1. Dimensionamento (de onde vêm os números)

| Grandeza | Conta | Valor |
|---|---|---|
| Itens de Limite | 80 M contas × 3–5 contratos | 240–400 M itens (~100–160 GB) |
| Requisições em voo no pico | Lei de Little: 5.000 TPS × ~50 ms | ~250 |
| Escritas por autorização | **Medido na AWS** (seção 3): `limites` 1,09 + `autorizacoes` 3,06 + `autorizacoes-outbox` 6,12 + contabilização 2,14 + 2,14 | **~14,6 WRU** (a estimativa inicial, ~6–8, esquecia o outbox e a contabilização) |
| WRU no pico | 5.000 × 14,6 | ~73 k WRU/s somando as 5 tabelas; a maior é o outbox, ~31 k/s (abaixo dos 40 k por tabela da quota padrão) |
| Eventos no pico | ~5.000 autorizações + ~500 recusas | ~5,5 k PutEvents/s (quota aprovada: **6.000**) |
| Invocations no pico | 5,5 k × 3 regras | ~16,5 k/s (quota aprovada: **20.000**) |
| Teto por contrato | 1.000 WCU/s por partição, com a guarda de 5 min no item (o custo por escrita cresce com a taxa) | ~8 op/s sustentadas (conta no ADR-0016) |
| API Gateway | 10.000 RPS / burst 5.000 por conta e região | O pico usa ~50% |

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
- `MAX_EM_VOO` alto (256) só troca 503 rápido por fila lenta: com 256 em voo e ~95 rps por task, a espera chegava a segundos. Dimensionado agora como capacidade × p99 alvo × 2 ≈ **32** por task (`var.max_em_voo`).
- O target tracking de CPU (60%, 3 pontos de 1 min) pediu a 5ª e a 6ª task **~6 min depois do início** da sobrecarga. Reativo não segura pico súbito: para o pico previsível (início de mês, Black Friday), mínimo agendado ou predictive scaling (ADR-0004/0013). O shedding protege o que já está no ar enquanto isso.

**Extrapolação para 5.000 TPS** (pico sustentado; ARM64 não foi medido, a conta é x86):

| Alvo | autorizacao | limites | Total |
|---|---|---|---|
| CPU a 60% (vazão; p99 de centenas de ms) | ~68 vCPU | ~38 vCPU | ~106 vCPU |
| CPU a ~33% (p99 de gateway ≤ ~165 ms, perto do RNF de 150 ms) | ~130 vCPU | ~72 vCPU | ~200 vCPU |

O RNF de p99 ≤ 150 ms **não foi atingido com folga** em nenhum degrau; o mais perto foi 164 ms no gateway a 33% de CPU. A distância é Python mais a transação de ~21 ms. Fechar isso pede a alavanca 1 da seção 5 (tira a transação do caminho comum) e, se ainda faltar, um runtime compilado nos dois serviços quentes (ADR-0003).

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
