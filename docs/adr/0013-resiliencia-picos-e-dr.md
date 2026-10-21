# ADR-0013 — Resiliência, picos previsíveis e imprevisíveis, e DR

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-04, PAP-01 (ver também `docs/arquitetura/well-architected.md`)

## Contexto

Os picos podem ser previsíveis (início de mês, datas de pagamento) ou imprevisíveis (campanhas, Black Friday). O SLO é de 99,9% (ADR-0001). O Fargate leva ~109 s para escalar reativamente. O DynamoDB on-demand de uma tabela nova aguenta 4.000 writes/s e dobra o pico a cada 30 min. Não há consistência forte multi-região a partir de sa-east-1.

## Decisão

### Contenção de falha (dentro da região)

| Mecanismo | Onde | Efeito |
|---|---|---|
| Timeouts curtos com orçamento | Cliente HTTP do `autorizacao` (connect 0,5 s, read 1,5 s); boto3 (1 s / 2 s, 3 tentativas `standard`) | Uma dependência lenta não segura threads |
| Retry com backoff "full jitter" | Gateway do `limites` (2 tentativas) | Só porque **toda operação é idempotente** |
| **Circuit breaker** | `adapters/outbound/disjuntor.py`: abre com 5 falhas seguidas, pausa de 5 s, meio-aberto com 1 prova | Fail-fast → **503 + Retry-After**; o `limites` respira. Coberto por teste de regressão: 4xx na prova não deixa o disjuntor preso |
| **Load shedding** | Middleware do `autorizacao` (`MAX_EM_VOO`) | Acima da capacidade, 503 imediato em vez de fila e timeout em cascata; `/saude` é isento |
| Bulkhead por fila | Uma fila SQS + DLQ por consumidor | Consumidor quebrado não afeta os outros nem o produtor |
| Multi-AZ | ECS em 2 AZs; DynamoDB, SQS e EventBridge regionais | Perda de uma AZ sem indisponibilidade |
| Deploy seguro | ECS deployment circuit breaker com rollback | Versão que não fica saudável volta sozinha |

### Picos

- **Previsíveis** (na medição, o target tracking pediu tasks ~6 min depois do início da sobrecarga, `docs/capacidade.md` seção 3):
  - ***scheduled scaling* das tasks antes da janela: implementado.** `var.janelas_de_pico` (módulo `ecs_servico`) sobe o mínimo no início e o devolve no fim, no fuso de Brasília. O sandbox aplica o exemplo do dia 1 de cada mês, 06:30–12:00, mínimo de 4 tasks em `autorizacao` e `limites` (base 2). Um apply dentro da janela devolve o mínimo de base (runbook R3);
  - **warm throughput do DynamoDB a 2× o pico: implementado, desligado no sandbox.** `var.warm_throughput` (módulo `dados`) por tabela e por GSI. Fica desligado porque aumentar é cobrado e **não pode ser reduzido depois**. Valores de produção, das escritas medidas por autorização: `limites` 11.000 escritas/s, `autorizacoes` 31.000 e o `gsi_orfas` 20.000, `autorizacoes-outbox` 61.000. Este último passa da quota padrão de 40.000 WRU/s por tabela: pedir a quota antes, ou eliminar o outbox (alavanca 1 de `docs/capacidade.md`). Esses valores vêm do DEBITO; com captura no mix, o pico do outbox já é ~49.000 WRU/s no mix de cartão (2× ≈ 98.000) e ~64.000 no pior caso (2× ≈ 129.000), então a quota por tabela sobe junto com o warm throughput (`docs/capacidade.md` §1.6, derivado do código, não medido). **Ligado uma vez, na janela de 5.000 rps de 21/10** (`docs/capacidade.md` §3.1), com valores abaixo dos de produção: `autorizacoes-outbox` 32.000, `autorizacoes` 17.000 e `gsi_orfas` 11.000, `limites` 10.000, `contabilizacao-lancamentos` e `contabilizacao-inbox` 11.500 cada (custo estimado ~US$ 67). Resultado: estrangulamento só em `autorizacoes`, no minuto da chegada ao patamar, e zero no resto. Os valores ficam nas tabelas até o destroy;
  - **predictive scaling em `ForecastOnly`: implementado** em `autorizacao` e `limites` (`var.modo_escala_preditiva`), com o par `ECSServiceCPUUtilization` e o mesmo alvo de 60% do target tracking, que continua agindo. Só gera previsão: compara-se a previsão com o pico real antes de passar a `ForecastAndScale`. Precisa de pelo menos 24 h de métrica (no sandbox efêmero pode nem aparecer). Sem custo adicional.
- **Imprevisíveis:**
  - mínimo de tasks a 60% de utilização no pico esperado, ou seja, **1,6× de folga** para atravessar os ~109 s do reativo;
  - acima disso, load shedding e WAF;
  - **429 e 503 com `Retry-After` são o comportamento projetado:** o sistema não quebra nem corrompe nada. Medido a 5.000 rps oferecidos com a frota fixa em 60 tasks de 1 vCPU por serviço e a CPU a ~85%: 3,8% de 503, nenhum 500, e toda aprovação chegou ao lançamento contábil (`docs/capacidade.md` §3.1). Para o SLO, o 503 conta como falha e consome error budget, porque é capacidade que faltou; o 429 de cota do consumidor não conta (`docs/operacao/slo.md`).
- **Teto de borda:** API Gateway com 10.000 RPS por conta e região. O pico de 5.000 usa metade. Conta dedicada por domínio. Tetos por consumidor em produção: `docs/capacidade.md`, seção 1.4.

### DR (desenhado, não implementado — Won't no MoSCoW)

- **Ativo-passivo:** global tables (MREC) replicando para uma segunda região, **escrita só na primária**. O last-writer-wins proíbe ativo-ativo no saldo.
- **Failover manual com fencing:**
  1. bloqueio de escrita na primária (SCP/flag);
  2. promoção da secundária;
  3. reconciliação de RPO.
- **RPO de segundos (> 0):** autorizações aprovadas nos últimos segundos podem não ter replicado. O reconciliador e a reconciliação com o legado fecham a diferença.
- **Regulatório:** não existe segunda região AWS no Brasil, então DR fora do país reativa os arts. 15–17 da Res. CMN 4.893 (comunicação e convênio).

## Consequências

- **Positivas:**
  - Falhas são contidas e explícitas (503/429 com instrução de retry).
  - O comportamento sob sobrecarga é previsível.
  - O DR é honesto sobre o RPO.
- **Negativas:**
  - Folga de capacidade paga o tempo todo.
  - O DR não implementado é uma limitação declarada.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Exigência de RPO = 0 ou 99,99% | Arquitetura celular por faixa de contratos + stand-in (autorização offline com limite reduzido) durante falha, ou consistência forte multi-região quando a região suportar |
| Picos imprevisíveis maiores que a folga com frequência | Pré-aquecimento orientado por sinal de negócio (campanhas avisadas pelo marketing) |

## Referências

- AWS Builders' Library — Timeouts, retries and backoff with jitter: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
- AWS Builders' Library — Using load shedding to avoid overload: https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/
- DynamoDB warm throughput: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/warm-throughput.html
- Application Auto Scaling, scheduled scaling: https://docs.aws.amazon.com/autoscaling/application/userguide/application-auto-scaling-scheduled-scaling.html
- Global tables (consistência): https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/V2globaltables_HowItWorks.html
