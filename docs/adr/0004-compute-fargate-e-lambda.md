# ADR-0004 — ECS Fargate no caminho síncrono, Lambda nos consumidores assíncronos

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** MS-02, RN-04

## Contexto

O caminho síncrono (API Gateway → `autorizacao` → `limites` → DynamoDB) precisa de p99 ≤ 150 ms a 5.000 TPS (ADR-0001). O trabalho assíncrono (contabilização, relay do outbox, reconciliador) é orientado a evento e tem rajadas.

Fatos (`01-pesquisa/02-aws-servicos-e-quotas.md`):

- **Lambda:**
  - limita RPS a 10 × concorrência, então 5.000 TPS exigem ≥ 500 de concorrência por função;
  - escala 1.000 ambientes a cada 10 s;
  - cold start em < 1% das invocações;
  - SnapStart e provisioned concurrency são mutuamente exclusivos.
  - **A conta tem 1.000 de concorrência em vigor** (`docs/operacao/infraestrutura.md`, seção 2). Pela regra dos 10×, 1.000 de concorrência sustentam até 10.000 RPS (a quota é da conta, dividida entre as funções), o dobro do pico: **a quota não impede Lambda no síncrono**.
- **ECS Fargate:** o scale-out reativo leva ~86 s para disparar e ~109 s até servir no melhor caso documentado. Predictive scaling existe em sa-east-1. A conta tem 140 vCPU de quota.
- **EKS:** plano de controle pago, mais operação (upgrades, add-ons). O App Mesh foi descontinuado em 30/09/2026.

## Decisão

- **`autorizacao` e `limites`: ECS Fargate, ARM64 (Graviton).**
  - O sandbox implantado pela esteira roda X86_64 (`TF_VAR_arquitetura_cpu`): o runner do GitHub é x86, e o build ARM64 exigiria emulação. O ARM64 não foi medido (`docs/capacidade.md`).
  - Processo longo, sem cold start, pool de conexões HTTP/DynamoDB quente.
  - Custo previsível em carga sustentada.
  - Na frente: NLB interno com VPC Link (v1) a partir do API Gateway REST; east-west por ECS Service Connect (ADR-0012).
- **`contabilizacao`, relay do outbox e sweeper: Lambda.** O **reconciliador** não é Lambda: roda como **serviço ECS** (mesma imagem da `autorizacao`), porque precisa do Service Connect para chamar o `limites` e Lambda não participa dele (ADR-0012).
  - SQS/Streams como gatilho, escala a zero, resposta parcial em lote.
  - O mesmo núcleo roda como container *poller* no ambiente local: só o adapter de entrada muda (hexagonal).
- **Picos (RN-04):**
  - **Previsíveis** (início de mês, pagamentos): o target tracking pediu as tasks extras ~6 min depois do início da sobrecarga (`docs/capacidade.md`, seção 3), então a capacidade sobe **antes** da janela:
    - **scheduled scaling, implementado:** `var.janelas_de_pico` no módulo `ecs_servico` cria um `aws_appautoscaling_scheduled_action` que sobe o mínimo no início da janela e outro que o devolve no fim. O sandbox tem o exemplo do dia 1 de cada mês, 06:30–12:00 de Brasília, mínimo de 4 tasks em `autorizacao` e `limites`;
    - **warm throughput no DynamoDB a 2× o pico, implementado e desligado no sandbox:** `var.warm_throughput` no módulo `dados` (tabela e GSI). Aumentar é cobrado e não pode ser desfeito; os valores de produção estão no ADR-0013;
    - **predictive scaling, só desenhado:** precisa de histórico de carga que o sandbox não tem.
  - **Imprevisíveis** (campanha, Black Friday): mínimo de tasks dimensionado para 60% de utilização no pico esperado, ou seja, 1,6× de folga para atravessar os ~109 s do reativo.
  - **Proteção:** load shedding por concorrência máxima por task (503 + `Retry-After`) e WAF rate-based.

## Consequências

- **Positivas:** latência estável no síncrono, sem cold start na cauda; pagar por uso no assíncrono; mesmo código de domínio nos dois.
- **Negativas:**
  - Headroom mínimo pago o tempo todo.
  - Dois modelos de deploy (ECS e Lambda).
  - O escalonamento do Fargate não reage em segundos.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Tráfego síncrono muito intermitente (longos períodos perto de zero) | Lambda com provisioned concurrency fica mais barato. Reavaliar com dados de custo |
| Plataforma do banco padroniza EKS (paved road com observabilidade, segurança e políticas) | Migrar: as imagens são as mesmas, só muda o orquestrador |
| Picos imprevisíveis maiores que a folga de 1,6× com frequência | Arquitetura celular ou fila na borda com resposta assíncrona para canais que aceitarem |

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Lambda no síncrono | Não é a quota: a conta tem 1.000 em vigor, que cobre o pico. É **o p99**: cold start em < 1% das invocações cai exatamente na cauda que o RNF de 150 ms mede, e evitá-lo pede provisioned concurrency de ≥ 500 por função, paga o tempo todo. É **o custo em carga sustentada**: com tráfego alto e contínuo, cobrança por invocação e duração perde para tasks com CPU ocupada. E é **o Service Connect**: `autorizacao` chama `limites` (e o reconciliador também) pelo Service Connect, do qual Lambda não participa; trocar exigiria pôr o `limites` atrás de um ALB ou do VPC Lattice |
| EKS | Mais operação sem ganho para 3 serviços; vira a escolha certa se for o padrão de plataforma |

## Referências

- Lambda scaling: https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html
- ECS predictive scaling: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/predictive-auto-scaling.html
- App Mesh end of support: https://docs.aws.amazon.com/app-mesh/latest/userguide/what-is-app-mesh.html
