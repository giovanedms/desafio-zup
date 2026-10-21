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
| RN-02 | Reserva consistente sob concorrência | ADR-0005; `limites/tests/contract/test_porta_repositorio.py` (corrida de 100 reservas em 32 threads, memória **e** DynamoDB); property tests; demo §2 (100 reservas, 50 clientes em paralelo). **Hold como árbitro** de captura, estorno e expiração (ADR-0020): 20 capturas + 10 liberações concorrentes → exatamente 1 aplicada (`test_captura_estorno_e_expiracao_disputam_o_hold_e_so_um_vence`); demo §4: 10 capturas × 10 estornos → `{201: 1, 409: 19}` | **D** |
| RN-03 | Eventos de domínio para consumidores desacoplados | ADR-0007/0009; `relay/tests/integration`; `contabilizacao/tests/integration`. Ciclo de vida da reserva (ADR-0020): captura (`CAPTURAR_RESERVA`, `ReservaCapturada`) e expiração com motor (`ExpirarReservas`, `ReservaExpirada`): `autorizacao/tests/unit/test_autorizar_transacao.py`, `test_reconciliador.py`, `tests/contract/test_vencimento_de_reservas.py`; demo §4 | **D** |
| RN-04 | Picos previsíveis e imprevisíveis | **Imprevisível:** load shedding testado (`test_load_shedding_acima_do_maximo_em_voo_e_503`), `MAX_EM_VOO` derivado da CPU da task, target tracking. **Previsível:** scaling agendado no Terraform (`aws_appautoscaling_scheduled_action`, janela de início de mês no sandbox; `run "ecs_servico_com_janela_de_pico"` no tftest) e `warm_throughput` suportado no módulo `dados` (desligado no sandbox por custo; `run "dados_com_warm_throughput_configurado"`). Predictive scaling e a folga de 1,6× só no ADR-0013 | **I** (Terraform testado, sem pico real executado) + **Dc** |
| RN-05 | Core legado via ACL | ADR-0014; o legado é dono de concedido, status, conta e produto, e a ACL aplica os quatro com compare-and-set por versão; `limites/adapters/inbound/legado/`; `tests/unit/test_legado.py`; `legado-mock` + teste de contrato do layout. Ids limitados ao layout (20 e 16 posições): `formatar` falha em vez de truncar (`test_formatar_falha_em_vez_de_truncar`, inclusive id com `\n` final: validação por `fullmatch`) e `traduzir` rejeita id fora do pattern (DLQ) | **D** |
| RN-06 | Auditoria, rastreabilidade, idempotência | ADR-0006/0017; idempotência com testes de replay, commit incerto e estorno concorrente; correlação propagada no caminho da autorização até o evento (o `LimiteAlterado` sai com correlação sintética e sem trace, ADR-0017); trilha WORM (sandbox: CloudTrail Object Lock, criado no bootstrap da conta, fora do Terraform do repositório). Guarda de 5 min com compactação de carona (ADR-0005/0006); prazo de reaplicação de 4 min e `RECONCILIACAO_MANUAL` (`test_retomada_fora_do_prazo_nao_reenvia_ao_limite`, `test_orfa_fora_do_prazo_vai_para_reconciliacao_manual_sem_tocar_o_limite`); runbook R7 | **D** (idempotência, rastreabilidade) + **I** (trilha WORM: CloudTrail com Object Lock criado no bootstrap da conta, fora da IaC) |
| RN-07 | Múltiplos tipos de contrato | `TipoProduto` (crédito, investimento, seguro, consórcio) na réplica do contrato e no layout legado. É rótulo: a regra de limite é a mesma para os quatro | **I** |

## Volumetria

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| NFR-01..02 | 80 M contas; 3–5 contratos/conta | ADR-0001; `docs/capacidade.md` §1 (sizing, key design) | **P/Dc** |
| NFR-03 | ~5.000 TPS | `docs/capacidade.md` §3–5: medido na AWS (20/10) até ~570 rps aceitos (600 oferecidos, 4,4% de 503) com 5.000 contratos, na imagem 1.0.3, anterior às correções de 21/10 (não remedida, `docs/capacidade.md` §3); ~8 ms de CPU por autorização (~122 rps/vCPU), ~4,6 ms por operação no limite; 150 rps com p99 de gateway de 164 ms; extrapolação para 5.000 TPS (~106–200 vCPU) e custo (14,6 WRU de tabela medidos por DEBITO; captura e recusa derivadas do código, `docs/capacidade.md` §1.6). **5.000 TPS não foi executado** (custo e quota do sandbox); p99 ≤ 150 ms não atingido | **D** (medido em escala menor) + **Dc** (extrapolação) + **L** (5.000 TPS não executado) |
| NFR-04 | Latência, SLO, RPO/RTO, retenção | ADR-0001 | **P** |

## Contrato de API

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| API-01 | `POST /v1/contratos/{id}/autorizacoes` | `contracts/openapi/autorizacao-v1.yaml` | **D** |
| API-02 | `Idempotency-Key` obrigatório | ausente/inválida → 400 (teste) | **D** |
| API-03 | Campos e validações | `PedidoRequest` + `PedidoDeAutorizacao`; testes 400/422; corpo de **requisição** validado contra `PedidoDeAutorizacao` nas duas direções (aceito pelo serviço ⇒ válido no contrato; `null` em opcional → 422; nome repetido → 400) e corpos de **resposta** validados contra o OpenAPI (`services/autorizacao/tests/contract/test_api_bate_com_o_contrato.py`). `id_conta` é obrigatório na API pública, e o `autorizacao` o repassa em toda operação pedida pelo canal; o `limites` o confere contra a titular do contrato na réplica do core: divergente → 422 `CONTA_DIVERGENTE`, sem efeito no limite (`Limite.atende`/`diagnosticar`, condição na escrita; testes em memória e no DynamoDB); a autorização fica registrada como recusada, sem os saldos. **A conferência só acontece quando o chamador manda `id_conta`:** na API interna do `limites` o campo é opcional em todo `tipo`, e sem ele nada é conferido (`test_sem_id_conta_nada_muda`). A expiração e a compensação o omitem de propósito (portabilidade); um chamador interno que o omita numa operação de canal não é barrado pelo `limites`. **Desvios declarados:** (1) o enunciado descreve `metadata` como objeto livre ("rastreio, geo"); aqui ele é uma **allowlist** (`canal`, `id_rastreio_canal`, `geo.uf`, `geo.municipio_ibge`) por minimização da LGPD, e campo fora dela dá 422 apontando o campo (ADR-0011, ADR-0017). Campo novo entra no schema de forma aditiva. (2) O enunciado pede `moeda` em ISO 4217 ("ex: BRL"); aqui o formato é 3 letras maiúsculas e só `BRL` é aceito: outro código válido dá 422 com `campo` = `moeda` (`PedidoDeAutorizacao`, `test_moeda_diferente_de_brl_e_422_de_dominio`; premissa do ADR-0001, "valor da transação em BRL") | **D** |
| API-04 | 201 com `id_autorizacao` e saldo reservado | teste + demo | **D** |
| API-05 | 200 replay | corpo idêntico byte a byte (teste + demo) | **D** |
| API-06 | 402 limite insuficiente | teste + demo; nuance no ADR-0011 | **D** |
| API-07 | 409 conflito | em andamento / conflito de estado (testes); **não existe 409 de contenção** (ADR-0005). Captura ou estorno que perde o hold → 409 `conflito-de-estado` (ADR-0020; demo §4); a que VENCE o hold conclui mesmo se o expirador encaminhou a reserva no meio (`test_vencedor_do_hold.py`); retomada fora do prazo → 409 `conflito-de-estado` **sem** `Retry-After` (`EmReconciliacao`; ADR-0006) | **D** |
| API-08 | 422 validação de domínio | testes unitários e de conformidade (8 corpos inválidos); o serviço é a autoridade também na AWS (o gateway só valida parâmetros) e o smoke exige 422 `problem+json` na AWS (`tests/e2e/aws.py borda`) | **D** |
| API-09 | 429 throttling por cliente/plano | usage plans por canal (3 planos) + WAF rate-based por consumidor (`x-api-key`) e por IP no Terraform; `THROTTLED`/`QUOTA_EXCEEDED` em problem+json com `Retry-After`; o smoke exige um 429 numa rajada concorrente pelo `canal-app` (`tests/e2e/aws.py borda`). O 429 do WAF sai em `application/json` (desvio no catálogo) | **D** (gateway: 429 observado no smoke na AWS, 20/10) + **I** (WAF: só configurado e asserido no tftest) |
| API-10 | 503 degradação | circuit breaker e load shedding (testes). Estrangulamento do DynamoDB no `limites` → 503 `servico-indisponivel` com `Retry-After: 1` (`limites/tests/unit/test_dynamodb_resiliencia.py`; ADR-0005/0016). O mesmo no `autorizacao`: estrangulamento, timeout e conflito de transação do DynamoDB → 503 com `Retry-After: 1` e repetição com a mesma chave (`autorizacao/tests/unit/test_dynamodb_resiliencia.py`) | **D** |
| API-11 | Idempotência em toda operação mutante | POST de autorização; operações internas do `limites` idempotentes por `id_operacao` | **D** |
| API-12 | Versionamento e depreciação | `/v1`; política no ADR-0011 (RFC 9745 + 8594); oasdiff e gate de schemas na CI em push e PR, fail-closed (`scripts/compat_contratos.sh`). Headers `Deprecation`/`Sunset`: só na política, não há versão depreciada para exercitar | **D** (compatibilidade) + **Dc** (depreciação) |
| API-13 | Erros padronizados (RFC 7807 → 9457) | `libs/plataforma/problemas`; catálogo; testes de content-type e `codigo` | **D** |
| API-14 | OpenAPI 3.x contract-first | contratos validados (Spectral + spec-validator) na CI; código ≡ contrato por teste (`test_api_bate_com_o_contrato.py`: status declarado, media type, schema e catálogo); inclui os 4 `tipo_operacao`, o 405, o 422 do GET, o 503 do banco e `CONTA_DIVERGENTE`; headers declarados, 201/200 nos 4 tipos e replay de recusa conferidos; o contrato **interno** é conferido dos dois lados: fornecedor (`services/limites/tests/contract/test_api_interna_bate_com_o_contrato.py`) e consumidor (`services/autorizacao/tests/contract/test_limites_http_bate_com_o_contrato_interno.py`); a spec gerada pelo FastAPI está desligada (`/openapi.json` → 404) | **D** |
| API-15 | Contrato usado para fronteiras e erros | ADR-0002/0011; contrato interno separado | **Dc** |

## Contrato de evento

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| EVT-01 | Schema `TransacaoAutorizada` v1 | `contracts/events/`; teste de contrato do produtor. A captura publica `TransacaoAutorizada` (DEBITO, com `id_autorizacao_original` opcional) e `ReservaCapturada` (`reserva-capturada.v1.schema.json`; `test_captura_cumpre_o_schema_da_transacao_autorizada_com_a_original`; ADR-0020) | **D** |
| EVT-02 | EventBridge e roteamento | bus `financas` + regras (bootstrap e Terraform); teste de integração | **D** |
| EVT-03 | Consumidores desacoplados | filas por consumidor + DLQ; fan-out provado na demo §6 (o mesmo CloudEvent em `antifraude`, `notificacoes` e no lançamento contábil). Só a contabilização tem consumidor implementado; antifraude e notificações são filas dos sistemas a jusante. **Data platform** (o 4º consumidor do enunciado) não tem fila: na produção é o archive do bus mais Firehose → S3; no sandbox, só o archive (ADR-0009, seção "Decisão") | **D** (fan-out) + **I** (consumidores) |
| EVT-04 | Imutáveis, passado, CloudEvents | CloudEvent em `detail`; validação por schema | **D** |
| EVT-05 | `event_id` para dedup | `id` estável gerado no outbox; inbox da contabilização (teste de reentrega e corrida); o item do outbox não expira por idade e o sweeper republica o que sobrou em páginas, até 10.000 por rodada (`relay/src/relay/sweeper.py`) | **D** |
| EVT-06 | Evolução validada por registry | `scripts/compat_schemas.py` + `tests/contract/test_compat_schemas.py` (inclui `additionalProperties`, `pattern`, limites numéricos, `format` e tamanhos); inclui `enum`/`const` novos e composições (`allOf`, `anyOf`, `oneOf`, `not`, `dependencies`); gate na CI em push e PR, fail-closed; quebra deliberada só com registro em `contracts/events/quebras-aceitas.json` (motivo e data; ADR-0008). **Nuance:** o registry do EventBridge não valida (ADR-0008); o repositório + CI fazem esse papel | **D** |
| EVT-07 | Consumidor idempotente, tolera reordenação | property test da contabilização (qualquer ordem e reentrega → mesmo saldo) | **D** |
| EVT-08 | `LimiteAlterado` e estorno | schemas + produtores (CDC e autorização); `LimiteAlterado` conferido na fila `notificacoes` na demo §7 | **D** |

## Arquitetura, microsserviços, EDA e API management

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| ARQ-01..03 | SOLID, Clean Code, hexagonal | ADR-0019; marcadores SRP/OCP/LSP/ISP/DIP no código; import-linter na CI com 9 contratos (3 serviços + `relay` + `plataforma`, também atravessada: import indireto do SDK via `plataforma` quebra o contrato do serviço); `tests/contract/test_gate_de_arquitetura.py` prova que um `import boto3` no domínio deixa o contrato `BROKEN` | **D** |
| ARQ-04 | DDD | ADR-0002; `docs/dominio/`; agregados com invariantes testadas | **D** |
| ARQ-05 | CQRS, Repository, Domain Events | ADR-0015 | **Dc** |
| MS-01 | Fronteiras por BC | ADR-0002; a fronteira `autorizacao → limites` tem contrato interno com teste de conformidade dos dois lados (fornecedor e consumidor) e segurança declarada (só `autorizacao` e `reconciliador` alcançam a porta, por security group) | **Dc** + **D** (contrato interno) |
| MS-02 | Runtime de compute justificado | ADR-0004 (p99 e cold start, custo em carga sustentada, Service Connect; quota de Lambda de 1.000) + medição em `docs/capacidade.md` §3 | **Dc** |
| MS-03 | Database-per-service | uma tabela (ou mais) por serviço; IAM por serviço verificado no tftest (`run "task_roles_menor_privilegio"`: allowlist exclusiva de recursos nas 4 task roles) | **D** |
| EDA-01 | EventBridge, SNS, SQS | EventBridge + SQS implementados; ADR-0009 pesa SNS (inclusive regra → SNS → N filas) e diz quando entraria; SNS usado nos alarmes | **Dc/I** |
| EDA-02 | SQS, Kinesis, MSK para volumetria e ordem | ADR-0009 | **Dc** |
| APIM-01 | API Gateway com autorização | Terraform (REST + Cognito + API key); o smoke exige 401 `problem+json` sem token na AWS | **D** (sandbox) |
| APIM-02 | Rate limiting, usage plans, quotas | Terraform (3 planos com quota diária + WAF por consumidor e por IP); 429 exigido no smoke (API-09) | **D** (sandbox) |
| APIM-03 | Contract-first, versionamento | ADR-0011; gate de compatibilidade em push e PR; teste código ≡ contrato | **D** |
| APIM-04 | Gateway vs mesh vs BFF | ADR-0012 | **Dc** |

## Papel, IA e entrega

| ID | Requisito | Onde | Nível |
|---|---|---|---|
| PAP-01 | Well-Architected | `docs/arquitetura/well-architected.md`; ADR-0013/0017 | **Dc** |
| PAP-02 | Comunicar trade-offs | documento de arquitetura §1; diagramas em `docs/arquitetura/diagramas/`; ADRs com "quando deixa de ser boa" | **Dc** |
| IA-01..04 | Explicar decisões, trade-offs, código e alternativas | `docs/uso-de-ia.md`; `COMO-LER.md` nos 3 serviços; "Quando esta decisão deixa de ser boa" nos 20 ADRs; alternativas discutidas em todos, com seção formal em 8 (0003–0007, 0009, 0016, 0020) | **Dc** |
| ENT-01 | ZIP com código, IaC, diagramas, ADRs | `make zip` (`git archive HEAD`); 5 diagramas em `.mmd` e `.svg`; 20 ADRs | **I** |
| DEF-03 | Demo executando | `make up && make demo` (local, sem AWS); roda no job `e2e` da esteira a cada push e sai com 1 em qualquer divergência | **D** |

## Limitações declaradas

- **DR multi-região:** só desenhado (ADR-0013).
- **Reserva fragmentada** para contrato quente: só desenhada (ADR-0016). Teto do item único: ~8 op/s sustentadas por contrato; a observabilidade existe (alarme `OperacoesNaGuarda`, Contributor Insights).
- **Conclusão de `RECONCILIACAO_MANUAL`:** sem ferramenta administrativa no código; o procedimento é manual (runbook R7).
- **Exportação contábil** no formato do legado: desenhada (ADR-0014 §7).
- **OpenTelemetry completo:** evolução (ADR-0017).
- **mTLS entre serviços:** não ligado no sandbox.
- **API Gateway e Cognito não são emulados localmente:** verificados pelo smoke da esteira na AWS.
- **Consumidores de antifraude e notificações:** só as filas (sistemas a jusante).
- **Predictive scaling e warm throughput ligado:** desenhados; warm throughput fica desligado no sandbox por custo.
- **Saldo credor:** `utilizado` é saldo devedor líquido; devolver (CREDITO ou ESTORNO) mais do que o utilizado é recusado (`CREDITO_EXCEDE_UTILIZADO`).
- **p99 ≤ 150 ms:** não atingido (164 ms no melhor degrau, `docs/capacidade.md` §3); o alarme de latência fica em 400 ms.
- **Trilha de 10 anos:** desenho (Firehose → S3 Object Lock); no sandbox o durável é o archive de 30 dias, e `TransacaoCompensada` e `TransacaoEmReconciliacaoManual` não têm assinante (só o archive).
- **Autenticação na API interna:** não há; a fronteira é o security group (só `autorizacao` e `reconciliador`). Produção: mTLS do Service Connect.
- **`decimal(18,2)` nos schemas de evento:** `number` com teto; a escala de 2 casas é garantida pelo produtor (o EventBridge reserializa números).
