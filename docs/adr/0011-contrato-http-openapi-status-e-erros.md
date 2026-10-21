# ADR-0011 — Contrato HTTP: OpenAPI 3.0.3 contract-first, semântica de status, RFC 9457, dinheiro e depreciação

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** API-01, API-03, API-04, API-06, API-08, API-10, API-12, API-13, API-14, APIM-03

## Contexto

O enunciado dá o endpoint, os campos, a tabela de status (201/200/402/409/422/429/503), "RFC 7807 Problem Details", "OpenAPI 3.x contract-first" e "versionamento explícito com política de depreciação". A pesquisa das RFCs (em Referências) encontrou nuances. A postura é **atender ao pedido e registrar a nuance**.

## Decisões

### 1. OpenAPI **3.0.3**, contract-first

- O contrato (`contracts/openapi/`) é a fonte da verdade: o código e o gateway seguem o contrato, e não o contrário.
- **Por que 3.0.3 e não 3.1/3.2:** o API Gateway REST importa 2.0/3.0, não 3.1.
- **CI:** Spectral (lint) + `openapi-spec-validator` (estrutura) + **oasdiff** (breaking change contra a `main`).
- **Código ≡ contrato:** `services/autorizacao/tests/contract/test_api_bate_com_o_contrato.py` sobe o app com dublês e confere cada desfecho (201 e o replay 200 em cada um dos quatro `tipo_operacao` e na captura; 400, 401, 402, 404, 405, 409, 415, 422, 500, 503; o replay de recusa 402/404/409/422; e o GET com 200/401/404/405/422/503) contra a resposta declarada para aquele status: media type, headers de contrato emitidos (`Location`, `Idempotent-Replayed`, `Retry-After`, `Allow`, `WWW-Authenticate`) declarados na resposta, corpo no schema (jsonschema, Draft 4), nenhum membro fora do contrato e `codigo`/`type`/`status` iguais ao catálogo. Na outra direção, todo corpo de requisição que o serviço aceita é válido no schema `PedidoDeAutorizacao`: os de todo POST aprovado no arquivo e os de um corpus de variações (campos presentes e ausentes, bordas, `null`, extras); os exemplos do contrato são aceitos. O dublê do port `Limites` reproduz as pré-condições do agregado real (`Limite.diagnosticar`), na mesma ordem, inclusive `CONTRATO_INATIVO`; a diferença é a guarda de idempotência, que no dublê não expira (a janela de 5 min é testada no BC Limites). Exaustividade: todo `MotivoRecusa` tem código, detalhe e linha no enum do contrato, e todo problema do serviço tem linha igual no catálogo. Os desfechos só da borda (403, 429, 504) não passam pelo serviço; na AWS, o smoke `tests/e2e/aws.py borda` confere o 401 e o 429 da borda e o 422 do serviço (`valor: 0`).
- O serviço não publica a spec que o FastAPI gera (`/openapi.json`, `/docs`, `/redoc` desligados): ela divergia do contrato.

### 2. Status

| Situação | Status |
|---|---|
| Aprovada na primeira execução | 201 + `Location` **relativa** (`autorizacoes/{id}`) |
| Replay de aprovação | 200, com corpo **idêntico** e `Idempotent-Replayed: true` |
| Replay de recusa | **O mesmo status da original** (402/404/409/422), não 200, com corpo **idêntico** e `Idempotent-Replayed: true` |
| Limite insuficiente | **402**, mantido como no enunciado |
| Mesma chave em processamento | 409 `requisicao-em-andamento` |
| Estado mudou (estorno de estornada/expirada) | 409 `conflito-de-estado` |
| **Não existe 409 de contenção** | reservas no mesmo contrato são serializadas pelo banco (ADR-0005) |
| Sintaxe inválida | 400 (corpo que não é JSON, inclusive `NaN`/`Infinity`; objeto com nome repetido, que a RFC 7493 §2.3 proíbe e cada parser resolve de um jeito; `Idempotency-Key` ausente/inválida) |
| `X-Client-Id` ausente ou fora de `^[A-Za-z0-9._-]{1,64}$` (acesso direto ao serviço) | 401 com `WWW-Authenticate: Bearer` (RFC 9110 §15.5.2), como o 401 do gateway |
| Corpo fora do schema ou regra de domínio | 422 (inclui campo desconhecido, tipo errado, `null` em campo opcional, obrigatório ausente, valor ≤ 0, `metadata` fora da allowlist) |
| Contrato inexistente | 404 |
| Conta (`id_conta`) que não é a titular do contrato | 422 `CONTA_DIVERGENTE`, sem saldos na resposta |
| Rota inexistente / método não suportado (acesso direto ao serviço) | 404 `RECURSO_NAO_ENCONTRADO` (inclusive com barra final: sem redirect 307) / 405 `METODO_NAO_PERMITIDO` com `Allow`, em problem+json |
| Content-Type errado | 415 |
| Gateway | 400 (header obrigatório ausente), 401/403/429/504 |
| Rate limit | 429 só na borda (usage plan e WAF); o serviço não emite 429 |
| Falha inesperada | 500 |
| Fail-fast (circuit breaker / load shedding) | 503 + `Retry-After` |
| Banco de autorizações estrangulado, sem resposta ou em conflito de transação (depois dos retries do SDK) | 503 + `Retry-After: 1`; repetir com a mesma `Idempotency-Key` é seguro (id determinístico, escrita condicional, commit incerto idempotente pelo `ClientRequestToken`) |

**`Location` relativa.** O gateway publica o stage `v1` na frente do `/v1` do path. Uma referência absoluta (`/v1/contratos/...`) troca o path inteiro na resolução (RFC 3986 §5.2) e perde o stage; a relativa `autorizacoes/{id}`, resolvida contra a URI do POST, funciona com e sem prefixo.

**Correlação no replay.** O corpo de uma decisão (aprovação ou recusa) guarda o `correlation_id` e o `trace_id` da execução que a criou, então o replay é idêntico ao original; o header `X-Correlation-Id` é sempre o desta requisição.

**Nuance do 402.** A RFC 9110 §15.5.3 diz "reserved for future use". Os precedentes divergem: a Stripe usa 402, o **Open Finance Brasil usa 422** para `SALDO_INSUFICIENTE`, a Adyen usa 200 + `Refused`. Mantivemos o pedido. A recusa é registrada e publicada (`TransacaoRecusada`), e nas métricas **recusa de negócio não conta como erro** do SLO.

### 3. Erros

**RFC 9457**, sucessora da RFC 7807 citada no enunciado: mesmo formato e mesmo media type `application/problem+json`. Extensões:
- `codigo` (o "código" estável pedido pelo enunciado);
- `correlation_id`;
- `trace_id`;
- `erros[]` em validação, no máximo 20 itens (`erros_omitidos` diz quantos ficaram de fora: um corpo com milhares de campos extras não vira uma resposta maior que ele);
- `id_autorizacao` e `limite_disponivel` na recusa por limite (402).

Catálogo: `contracts/problem-types/catalogo.md`.

### 4. Dinheiro

**JSON number**, como no enunciado.
- Parse **exato** (`json.loads(parse_float=Decimal)`), `Decimal` no domínio e serialização exata (`simplejson(use_decimal=True)`).
- **Escala de 2 casas validada na aplicação.** O contrato não usa `multipleOf: 0.01`: validadores que dividem em ponto flutuante rejeitam valores válidos. Foi testado: `0.29 / 0.01 = 28.999999999999996`, então R$ 19,99 seria recusado.
- **Alternativa registrada:** string com pattern (RFC 7493 §2.2; Open Finance e Pix fazem assim). Ela elimina o risco de cliente JavaScript com valores acima de 2^53 centavos. Seria a escolha numa API aberta a terceiros.
- **Achado na demo, confirmado na AWS real (20/10/2026):** o EventBridge **reserializa os números** do `detail` como double.
  - LocalStack: `250.00` chega como `250.0`. AWS (amostra de ~500 números lidos crus da fila `notificacoes`): `1.00` chega como `1.0`, e `99999998.88` chega como **`9.999999888E7`** (notação científica).
  - O **valor** se preserva até ~15 dígitos significativos (o double faz ida e volta exata): `Decimal("9.999999888E7").quantize(Decimal("0.01"))` = `99999998.88`. Na LocalStack, `12345678901234.56` chegou exato.
  - Acima de 15 dígitos (R$ 10 trilhões ou mais com centavos) haveria perda. O `decimal(18,2)` do enunciado permite isso; nenhum limite ou transação real chega perto.
  - **Decisão revista:** v1 dos eventos continua com `number` (consumidor lê com `Decimal` e quantiza em 2 casas, nunca compara texto). A **v2** troca dinheiro por string decimal quando aparecer o primeiro consumidor que precise de mais de 15 dígitos ou de formato canônico (exportação regulatória, assinatura do payload). A API síncrona segue com `number`: ali o parse é nosso e exato.

### 5. Campos e regras do enunciado

- `id_contrato` aceito no corpo, como diz a tabela; se for diferente do path → 422.
- `id_conta` é **conferido**: toda operação pedida pelo canal (RESERVA, DEBITO e captura, CREDITO, ESTORNO) leva o `id_conta` ao BC Limites, que o compara com a conta titular gravada na réplica do contrato. Diferente → recusa `CONTA_DIVERGENTE` (422), registrada e publicada **sem saldos**: quem errou a conta não vê o saldo de um contrato alheio, nem no corpo, nem no GET, nem no `TransacaoRecusada`. As operações automáticas (expiração da reserva, compensação do reconciliador) **não** levam a conta: com a portabilidade a conta da réplica muda, e uma liberação com a conta antiga seria recusada e prenderia o hold.
- `moeda`: ISO 4217, e só BRL na v1.
- `metadata` com **allowlist** (`canal`, `id_rastreio_canal`, `geo.uf`, `geo.municipio_ibge`), sem dado pessoal.
- `id_autorizacao_original` obrigatório em ESTORNO. É campo **aditivo**: o enunciado não dá como referenciar a transação estornada.
- **Resposta:** `saldo_reservado` (semântica do enunciado: total reservado no contrato após a operação) mais `limite_disponivel`, `limite_utilizado` e `versao_limite`, aditivos, para eliminar a ambiguidade.

### 6. Versionamento e depreciação

- **Major no path** (`/v1`). Dentro do major, só mudanças aditivas, garantidas pelo oasdiff na CI.
- **Para depreciar:**
  - headers `Deprecation` (RFC 9745) + `Sunset` (RFC 8594) + `Link rel="deprecation"` apontando para o guia de migração;
  - **no mínimo 6 meses** entre Deprecation e Sunset;
  - uso por `client_id` monitorado nos access logs, e o sunset só acontece com o tráfego zerado ou com os donos dos canais avisados.

## Consequências

- **Positivas:** o contrato é verificável por máquina, as nuances estão documentadas e os erros são uniformes.
- **Negativas:**
  - **A autoridade do corpo é o serviço.** O gateway não valida o corpo (`validateRequestBody = false`; só a presença dos parâmetros obrigatórios): o validador draft-4 parcial dele respondia 400 onde o contrato promete 422, e o comportamento local divergia da AWS. O preço é o pedido inválido chegar ao serviço; ele é barrado ali antes de qualquer escrita.
  - O 400 de `Idempotency-Key` ausente muda de `codigo` conforme a camada: `REQUISICAO_MAL_FORMADA` no gateway, `IDEMPOTENCY_KEY_INVALIDA` no serviço (local). O status é o mesmo.
  - O 402 é heterodoxo.
  - Rota inexistente e método não suportado mudam de status conforme a camada: na AWS o gateway responde antes, 403 `SEM_PERMISSAO` (`MISSING_AUTHENTICATION_TOKEN`); no acesso direto ao serviço, 404 `RECURSO_NAO_ENCONTRADO` e 405 `METODO_NAO_PERMITIDO`. Os dois em problem+json do catálogo.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| API exposta a terceiros (Open Finance) | Dinheiro como string; recusa como 422 com código de negócio |
| Ferramental do gateway passa a importar 3.1 | Migrar o contrato para 3.1 (JSON Schema 2020-12) |

## Referências

- RFC 9457: https://www.rfc-editor.org/rfc/rfc9457 · RFC 9110 §15.5.3: https://www.rfc-editor.org/rfc/rfc9110#section-15.5.3
- RFC 9745 (Deprecation): https://www.rfc-editor.org/rfc/rfc9745 · RFC 8594 (Sunset): https://www.rfc-editor.org/rfc/rfc8594
- RFC 7493 §2.2 (números em I-JSON): https://www.rfc-editor.org/rfc/rfc7493#section-2.2
- Open Finance Brasil (Pagamentos): https://github.com/OpenBanking-Brasil/openapi
- oasdiff: https://github.com/oasdiff/oasdiff · Spectral: https://github.com/stoplightio/spectral
