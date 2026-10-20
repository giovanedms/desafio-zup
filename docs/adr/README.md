# Architecture Decision Records

Formato: MADR com uma seção obrigatória a mais, **"Quando esta decisão deixa de ser boa"** (o §4 do desafio pede discutir quando a solução não é ideal). Template em `0000-template.md`.

## Onde começar

Para entender a solução em ~15 min, leia nesta ordem:

1. **0005** — concorrência do limite (o coração do problema)
2. **0006** — idempotência ponta a ponta
3. **0002** — bounded contexts
4. **0007** e **0009** — eventos e mensageria

## Lista completa

| ADR | Decisão | Requisitos |
|---|---|---|
| [0001](0001-premissas-de-nfr.md) | Premissas de NFR que o enunciado não define (p99, SLO 99,9%, região, RPO/RTO, retenção) | NFR-01..04 |
| [0002](0002-bounded-contexts-e-servicos.md) | 3 bounded contexts; por que Limites é separado de Autorização | RN-01, RN-07, ARQ-04, MS-01, API-15 |
| [0003](0003-linguagem-python.md) | Python 3.13 (e como seria em Java/Kotlin) | IA-03 |
| [0004](0004-compute-fargate-e-lambda.md) | Fargate no síncrono, Lambda no assíncrono, EKS descartado | MS-02, RN-04 |
| [0005](0005-concorrencia-do-limite-item-unico.md) | **Escrita condicional de item único, sem transação no item quente** | RN-02, MS-03, API-04, API-07 |
| [0006](0006-idempotencia-ponta-a-ponta.md) | Id determinístico, lease, retomada e reconciliador | RN-06, API-02, API-05, API-11 |
| [0007](0007-outbox-transacional-e-relay.md) | Outbox na mesma escrita + relay próprio + sweeper | RN-03, EVT-05 |
| [0008](0008-contrato-de-evento-cloudevents.md) | CloudEvents em `detail`, versionamento e gate de compatibilidade | EVT-01, 04, 05, 06, 08 |
| [0009](0009-mensageria-eventbridge-e-sqs.md) | EventBridge + SQS por consumidor; SNS, FIFO, Kinesis e MSK avaliados | EDA-01, EDA-02, EVT-02, 03, 07 |
| [0010](0010-api-gateway-rest-cognito-usage-plans.md) | API Gateway REST, Cognito, usage plans, WAF, VPC Link → NLB | APIM-01, APIM-02, API-09 |
| [0011](0011-contrato-http-openapi-status-e-erros.md) | OpenAPI 3.0.3, semântica de status, RFC 9457, dinheiro, depreciação | API-*, APIM-03 |
| [0012](0012-api-gateway-vs-service-mesh-vs-bff.md) | Gateway no north-south, Service Connect no east-west, sem BFF | APIM-04 |
| [0013](0013-resiliencia-picos-e-dr.md) | Timeouts, retry, circuit breaker, load shedding, picos e DR | RN-04, PAP-01 |
| [0014](0014-acl-core-banking-legado.md) | ACL assíncrona e autoridade de dados por atributo | RN-05 |
| [0015](0015-padroes-taticos-repository-domain-events-cqrs.md) | Repository (2 formas), Domain Events, CQRS leve, sem Event Sourcing | ARQ-05 |
| [0016](0016-contrato-quente-reserva-fragmentada.md) | Teto por contrato e reserva fragmentada (escrow) como evolução | RN-02 |
| [0017](0017-observabilidade-auditoria-seguranca-compliance.md) | Rastreabilidade, trilha WORM, SLOs, segurança, compliance | RN-06, PAP-01 |
| [0018](0018-ambiente-de-demo-local-e-medicao-na-aws.md) | Demo local offline e medição de desempenho na AWS real | DEF-03 |
| [0019](0019-solid-clean-code-hexagonal.md) | SOLID, Clean Code e hexagonal: onde cada princípio está no código | ARQ-01..03 |
| [0020](0020-ciclo-de-vida-do-hold-captura-e-expiracao.md) | Captura (DEBITO da RESERVA), expiração com motor e o hold como árbitro | RN-01, RN-02, RN-03, API-07, EVT-01 |
