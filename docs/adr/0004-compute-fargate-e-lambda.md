# ADR-0004 — ECS Fargate no caminho síncrono, Lambda nos consumidores assíncronos

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** MS-02, RN-04

## Contexto

O caminho síncrono (API Gateway → `autorizacao` → `limites` → DynamoDB) precisa de p99 ≤ 150 ms a 5.000 TPS (ADR-0001). O trabalho assíncrono (contabilização, relay do outbox, reconciliador) é orientado a evento e tem rajadas.

Fatos (da documentação oficial da AWS; os links principais estão em Referências):

- **Lambda:**
  - limita RPS a 10 × concorrência, então 5.000 TPS exigem ≥ 500 de concorrência por função;
  - escala 1.000 ambientes a cada 10 s;
  - cold start em < 1% das invocações;
  - SnapStart e provisioned concurrency são mutuamente exclusivos.
  - **A conta tem 1.000 de concorrência em vigor** (`docs/operacao/infraestrutura.md`, seção 2). Pela regra dos 10×, 1.000 de concorrência sustentam até 10.000 RPS, divididos entre as funções da conta: um serviço quente no pico (≥ 500) cabe com folga, mas os dois (`autorizacao` e `limites`, 5.000 RPS cada) pediriam ≥ 1.000, a quota inteira, sem nada para relay e consumidores. A quota é ajustável: **ela não impede Lambda no síncrono, mas pede aumento antes**.
- **ECS Fargate:** o scale-out reativo leva ~86 s para disparar e ~109 s até servir no melhor caso documentado. Predictive scaling existe em sa-east-1. A conta tem 140 vCPU de quota.
- **EKS:** plano de controle pago, mais operação (upgrades, add-ons). O App Mesh foi descontinuado em 30/09/2026.

## Decisão

- **`autorizacao` e `limites`: ECS Fargate, ARM64 (Graviton).**
  - O sandbox implantado pela esteira roda X86_64 (`TF_VAR_arquitetura_cpu`): o runner do GitHub é x86, e o build ARM64 exigiria emulação. O ARM64 não foi medido (`docs/capacidade.md`).
  - Processo longo, sem cold start, pool de conexões HTTP/DynamoDB quente.
  - Custo previsível em carga sustentada.
  - Na frente: NLB interno com VPC Link (v1) a partir do API Gateway REST; east-west por ECS Service Connect (ADR-0012).
- **`contabilizacao`, relay do outbox, sweeper e os consumidores de referência de antifraude e notificações (`services/consumidores`): Lambda.** O **reconciliador** não é Lambda: roda como **serviço ECS** (mesma imagem da `autorizacao`), porque precisa do Service Connect para chamar o `limites` e Lambda não participa dele (ADR-0012).
  - SQS/Streams como gatilho, escala a zero, resposta parcial em lote.
  - O mesmo núcleo roda como container *poller* no ambiente local: só o adapter de entrada muda (hexagonal).
- **Picos (RN-04):**
  - **Previsíveis** (início de mês, pagamentos): o target tracking pediu as tasks extras ~6 min depois do início da sobrecarga (`docs/capacidade.md`, seção 3), então a capacidade sobe **antes** da janela:
    - **scheduled scaling, implementado:** `var.janelas_de_pico` no módulo `ecs_servico` cria um `aws_appautoscaling_scheduled_action` que sobe o mínimo no início da janela e outro que o devolve no fim. O sandbox tem o exemplo do dia 1 de cada mês, 06:30–12:00 de Brasília, mínimo de 4 tasks em `autorizacao` e `limites`;
    - **warm throughput no DynamoDB a 2× o pico, implementado e desligado no sandbox:** `var.warm_throughput` no módulo `dados` (tabela e GSI). Aumentar é cobrado e não pode ser desfeito; os valores de produção estão no ADR-0013;
    - **predictive scaling em `ForecastOnly`, implementado:** `var.modo_escala_preditiva` em `autorizacao` e `limites` (ADR-0013). Só gera previsão, sem agir; precisa de pelo menos 24 h de métrica, que o sandbox efêmero quase nunca tem.
  - **Imprevisíveis** (campanha, Black Friday): mínimo de tasks dimensionado para 60% de utilização no pico esperado, ou seja, 1,6× de folga para atravessar os ~109 s do reativo.
  - **Proteção:** load shedding por concorrência máxima por task (503 + `Retry-After`) e WAF rate-based.

## Consequências

- **Positivas:** latência estável no síncrono, sem cold start na cauda; pagar por uso no assíncrono; mesmo código de domínio nos dois.
- **Negativas:**
  - Headroom mínimo pago o tempo todo.
  - Dois modelos de deploy (ECS e Lambda).
  - O escalonamento do Fargate não reage em segundos.
  - **O p99 de 150 ms não foi atingido nesta stack:** 164 ms no gateway a 33% de CPU (`docs/capacidade.md`, seção 3). A distância é Python mais a transação de ~21 ms (`TransactWriteItems` da autorização com o outbox), que pesariam igual em Lambda: não é a escolha entre Fargate e Lambda. O caminho está na alavanca 1 de `docs/capacidade.md`, seção 5, e no runtime compilado do ADR-0003.
  - **Frente ao throughput medido (21/10, `docs/capacidade.md` §3.1):** 60 tasks Fargate de 1 vCPU por serviço receberam 5.000 rps oferecidos por 8 min e aceitaram 96,2%; o excesso saiu como 503 rápido do load shedding, sem 500. O que faltou foi CPU, não o modelo de compute: ~10 ms de CPU por requisição em cada serviço (derivado) levam a ~150–160 vCPU para aceitar tudo, acima da quota de 140, que precisa de aumento (R3). O mesmo volume em Lambda pediria ≥ 500 de concorrência por função (Contexto) e não mudaria a CPU por requisição. No assíncrono, o consumidor contábil com teto de 200 no ESM não acompanhou ~4.770 eventos/s: a escolha de Lambda ali se mantém; o teto de concorrência (ou o lote) precisa subir.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Tráfego síncrono muito intermitente (longos períodos perto de zero) | Lambda com provisioned concurrency fica mais barato. Reavaliar com dados de custo |
| Plataforma do banco padroniza EKS (paved road com observabilidade, segurança e políticas) | Migrar: as imagens são as mesmas, só muda o orquestrador |
| Picos imprevisíveis maiores que a folga de 1,6× com frequência | Arquitetura celular ou fila na borda com resposta assíncrona para canais que aceitarem |

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Lambda no síncrono | **Custo em carga sustentada:** com tráfego alto e contínuo, cobrança por invocação e duração perde para tasks com CPU ocupada, e tirar o cold start da cauda pede provisioned concurrency de ≥ 500 por função, paga o tempo todo. **Quota:** os dois serviços quentes pediriam a quota de concorrência inteira da conta (Contexto). **East-west:** `autorizacao` e reconciliador chamam o `limites` pelo Service Connect, do qual Lambda não participa; seria preciso expor o `limites` num listener do NLB interno que já existe, ou invocá-lo direto (`lambda:InvokeFunction`, se ele também virasse Lambda). Funciona, mas sai do padrão east-west do resto da plataforma (ADR-0012). **p99, argumento secundário:** o cold start (< 1% das invocações) cai na cauda que o RNF mede, mas não é o que separa esta stack dos 150 ms (Consequências). A favor da alternativa, e por isso ela continua no radar: escala mais rápido que o Fargate (1.000 ambientes a cada 10 s contra ~109 s) e poderia deixar a plataforma com um modelo de deploy só, em vez de dois |
| EKS | Mais operação sem ganho para 3 serviços; vira a escolha certa se for o padrão de plataforma |

## Referências

- Lambda scaling: https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html
- ECS predictive scaling: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/predictive-auto-scaling.html
- App Mesh end of support: https://docs.aws.amazon.com/app-mesh/latest/userguide/what-is-app-mesh.html
