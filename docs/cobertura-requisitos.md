# Cobertura dos requisitos do enunciado

**Níveis de evidência:**

| Nível | Significado |
|---|---|
| **D** — Demonstrado | Teste automatizado ou medição; aponta o arquivo |
| **I** — Implementado | Código existe e roda; sem teste dedicado àquele aspecto |
| **Dc** — Decisão | ADR justificando; a implementação não se aplica |
| **P** — Premissa | Assumida e declarada (o enunciado não define) |
| **L** — Limitação | Declarada; fora do escopo do prazo |

## Contexto de negócio

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| RN-01 | Autorização síncrona aprovação/recusa | `autorizacao` (API + caso de uso); `make demo` §1–3; `tests/unit/test_http_publico.py` | **D** |
| RN-02 | Reserva consistente sob concorrência | ADR-0005; `limites/tests/contract/test_porta_repositorio.py` (corrida de 100 threads, memória **e** DynamoDB); property tests; demo §2. **Hold como árbitro** de captura, estorno e expiração (ADR-0020): 20 capturas + 10 liberações concorrentes → exatamente 1 aplicada (`test_captura_estorno_e_expiracao_disputam_o_hold_e_so_um_vence`); demo §4: 10 capturas × 10 estornos → `{201: 1, 409: 19}` | **D** |
| RN-03 | Eventos de domínio para consumidores desacoplados | ADR-0007/0009; `relay/tests/integration`; `contabilizacao/tests/integration`. Ciclo de vida da reserva (ADR-0020): captura (`CAPTURAR_RESERVA`, `ReservaCapturada`) e expiração com motor (`ExpirarReservas`, `ReservaExpirada`): `autorizacao/tests/unit/test_autorizar_transacao.py`, `test_reconciliador.py`, `tests/contract/test_vencimento_de_reservas.py`; demo §4 | **D** |
| RN-04 | Picos previsíveis e imprevisíveis | ADR-0013 (scaling agendado/preditivo, warm throughput, folga 1,6×, load shedding); load shedding testado | **I/Dc** |
| RN-05 | Core legado via ACL | ADR-0014; `limites/adapters/inbound/legado/`; `tests/unit/test_legado.py`; `legado-mock` + teste de contrato do layout. Ids limitados ao layout (20 e 16 posições): `formatar` falha em vez de truncar (`test_formatar_falha_em_vez_de_truncar`) e `traduzir` rejeita id fora do pattern (DLQ) | **D** |
| RN-06 | Auditoria, rastreabilidade, idempotência | ADR-0006/0017; idempotência com testes de replay, commit incerto e estorno concorrente; correlação propagada; trilha WORM (sandbox: CloudTrail Object Lock, criado no bootstrap da conta, fora do Terraform do repositório). Guarda de 5 min com compactação de carona (ADR-0005/0006); prazo de reaplicação de 4 min e `RECONCILIACAO_MANUAL` (`test_retomada_fora_do_prazo_nao_reenvia_ao_limite`, `test_orfa_fora_do_prazo_vai_para_reconciliacao_manual_sem_tocar_o_limite`); runbook R7 | **D** |
| RN-07 | Múltiplos tipos de contrato | `TipoProduto` (crédito, investimento, seguro, consórcio) na réplica do contrato e no layout legado | **I** |

## Volumetria

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| NFR-01..02 | 80 M contas; 3–5 contratos/conta | ADR-0001; `docs/capacidade.md` §1 (sizing, key design) | **P/Dc** |
| NFR-03 | ~5.000 TPS | `docs/capacidade.md` §3–5: medido na AWS (20/10) até 600 rps com 5.000 contratos; ~8 ms de CPU por autorização (~122 rps/vCPU), ~4,6 ms por operação no limite; 150 rps com p99 de gateway de 164 ms; extrapolação para 5.000 TPS (~106–200 vCPU) e custo (14,6 WRU por autorização). **5.000 TPS não foi executado** (custo e quota do sandbox); p99 ≤ 150 ms não atingido com folga | **D** (medido em escala menor) + **Dc** (extrapolação) + **L** (5.000 TPS não executado) |
| NFR-04 | Latência, SLO, RPO/RTO, retenção | ADR-0001 | **P** |

## Contrato de API

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| API-01 | `POST /v1/contratos/{id}/autorizacoes` | `contracts/openapi/autorizacao-v1.yaml` | **D** |
| API-02 | `Idempotency-Key` obrigatório | ausente/inválida → 400 (teste) | **D** |
| API-03 | Campos e validações | `PedidoRequest` + `PedidoDeAutorizacao`; testes 400/422 | **D** |
| API-04 | 201 com `id_autorizacao` e saldo reservado | teste + demo | **D** |
| API-05 | 200 replay | corpo idêntico byte a byte (teste + demo) | **D** |
| API-06 | 402 limite insuficiente | teste + demo; nuance no ADR-0011 | **D** |
| API-07 | 409 conflito | em andamento / conflito de estado (testes); **não existe 409 de contenção** (ADR-0005). Captura ou estorno que perde o hold → 409 `conflito-de-estado` (ADR-0020; demo §4); retomada fora do prazo → 409 `conflito-de-estado` **sem** `Retry-After` (`EmReconciliacao`; ADR-0006) | **D** |
| API-08 | 422 validação de domínio | testes | **D** |
| API-09 | 429 throttling por cliente/plano | usage plans (`plano-restrito`) + WAF no Terraform; Gateway Response em problem+json | **I** (sandbox) |
| API-10 | 503 degradação | circuit breaker e load shedding (testes). Estrangulamento do DynamoDB no `limites` → 503 `servico-indisponivel` com `Retry-After: 1` (`limites/tests/unit/test_dynamodb_resiliencia.py`; ADR-0005/0016) | **D** |
| API-11 | Idempotência em toda operação mutante | POST de autorização; operações internas do `limites` idempotentes por `id_operacao` | **D** |
| API-12 | Versionamento e depreciação | `/v1`; política no ADR-0011 (RFC 9745 + 8594); oasdiff na CI | **Dc/I** |
| API-13 | Erros padronizados (RFC 7807 → 9457) | `libs/plataforma/problemas`; catálogo; testes de content-type e `codigo` | **D** |
| API-14 | OpenAPI 3.x contract-first | contratos validados (Spectral + spec-validator) na CI | **D** |
| API-15 | Contrato usado para fronteiras e erros | ADR-0002/0011; contrato interno separado | **Dc** |

## Contrato de evento

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| EVT-01 | Schema `TransacaoAutorizada` v1 | `contracts/events/`; teste de contrato do produtor. A captura publica `TransacaoAutorizada` (DEBITO, com `id_autorizacao_original` opcional) e `ReservaCapturada` (`reserva-capturada.v1.schema.json`; `test_captura_cumpre_o_schema_da_transacao_autorizada_com_a_original`; ADR-0020) | **D** |
| EVT-02 | EventBridge e roteamento | bus `financas` + regras (bootstrap e Terraform); teste de integração | **D** |
| EVT-03 | Consumidores desacoplados | filas por consumidor + DLQ; contabilização implementada; antifraude e notificações como filas assinantes | **I** |
| EVT-04 | Imutáveis, passado, CloudEvents | CloudEvent em `detail`; validação por schema | **D** |
| EVT-05 | `event_id` para dedup | `id` estável gerado no outbox; inbox da contabilização (teste de reentrega e corrida) | **D** |
| EVT-06 | Evolução validada por registry | `scripts/compat_schemas.py` + testes; gate na CI. **Nuance:** o registry do EventBridge não valida (ADR-0008) | **D** |
| EVT-07 | Consumidor idempotente, tolera reordenação | property test da contabilização (qualquer ordem e reentrega → mesmo saldo) | **D** |
| EVT-08 | `LimiteAlterado` e estorno | schemas + produtores (CDC e autorização) | **D** |

## Arquitetura, microsserviços, EDA e API management

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| ARQ-01..03 | SOLID, Clean Code, hexagonal | ADR-0019; import-linter na CI (contrato `BROKEN` testado) | **D** |
| ARQ-04 | DDD | ADR-0002; `docs/dominio/`; agregados com invariantes testadas | **D** |
| ARQ-05 | CQRS, Repository, Domain Events | ADR-0015 | **Dc** |
| MS-01 | Fronteiras por BC | ADR-0002 | **Dc/I** |
| MS-02 | Runtime de compute justificado | ADR-0004 | **Dc** |
| MS-03 | Database-per-service | uma tabela (ou mais) por serviço; IAM por serviço | **I** |
| EDA-01 | EventBridge, SNS, SQS | ADR-0009 (SNS avaliado e descartado com número) | **Dc/I** |
| EDA-02 | SQS, Kinesis, MSK para volumetria e ordem | ADR-0009 | **Dc** |
| APIM-01 | API Gateway com autorização | Terraform (REST + Cognito) | **I** (sandbox) |
| APIM-02 | Rate limiting, usage plans, quotas | Terraform (2 planos + WAF) | **I** (sandbox) |
| APIM-03 | Contract-first, versionamento | ADR-0011; CI | **D** |
| APIM-04 | Gateway vs mesh vs BFF | ADR-0012 | **Dc** |

## Papel, IA e entrega

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| PAP-01 | Well-Architected | `docs/arquitetura/well-architected.md`; ADR-0013/0017 | **Dc** |
| PAP-02 | Comunicar trade-offs | documento de arquitetura §1; ADRs com "quando deixa de ser boa" | **Dc** |
| IA-01..04 | Explicar decisões, trade-offs, código e alternativas | `docs/uso-de-ia.md`; `COMO-LER.md` por serviço; seção de alternativas em todo ADR | **Dc** |
| ENT-01 | ZIP com código, IaC, diagramas, ADRs | `make zip` | **I** |
| DEF-03 | Demo executando | `make up && make demo` (local, offline) | **D** |

## Limitações declaradas

- **DR multi-região:** só desenhado (ADR-0013).
- **Reserva fragmentada** para contrato quente: só desenhada (ADR-0016). Teto do item único: ~8 op/s sustentadas por contrato; a observabilidade existe (alarme `OperacoesNaGuarda`, Contributor Insights).
- **Conclusão de `RECONCILIACAO_MANUAL`:** sem ferramenta administrativa no código; o procedimento é manual (runbook R7).
- **Exportação contábil** no formato do legado: desenhada (ADR-0014 §7).
- **OpenTelemetry completo:** evolução (ADR-0017).
- **mTLS entre serviços:** não ligado no sandbox.
- **API Gateway e Cognito não são emulados localmente:** verificáveis na janela AWS.
