# ADR-0011 — Contrato HTTP: OpenAPI 3.0.3 contract-first, semântica de status, RFC 9457, dinheiro e depreciação

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** API-01, API-03, API-04, API-06, API-08, API-10, API-12, API-13, API-14, APIM-03

## Contexto

O enunciado dá o endpoint, os campos, a tabela de status (201/200/402/409/422/429/503), "RFC 7807 Problem Details", "OpenAPI 3.x contract-first" e "versionamento explícito com política de depreciação". A pesquisa (`01-pesquisa/03` §12) encontrou nuances. A postura é **atender ao pedido e registrar a nuance**.

## Decisões

### 1. OpenAPI **3.0.3**, contract-first

- O contrato (`contracts/openapi/`) é a fonte da verdade: o código e o gateway seguem o contrato, e não o contrário.
- **Por que 3.0.3 e não 3.1/3.2:** o API Gateway REST importa 2.0/3.0, não 3.1.
- **CI:** Spectral (lint) + `openapi-spec-validator` (estrutura) + **oasdiff** (breaking change contra a `main`).

### 2. Status

| Situação | Status |
|---|---|
| Aprovada na primeira execução | 201 + `Location` |
| Replay de aprovação | 200, com corpo **idêntico** e `Idempotent-Replayed: true` |
| Replay de recusa | **O mesmo status da original** (402/422), não 200 |
| Limite insuficiente | **402**, mantido como no enunciado |
| Mesma chave em processamento | 409 `requisicao-em-andamento` |
| Estado mudou (estorno de estornada/expirada) | 409 `conflito-de-estado` |
| **Não existe 409 de contenção** | reservas no mesmo contrato são serializadas pelo banco (ADR-0005) |
| Sintaxe inválida | 400 (JSON malformado, `Idempotency-Key` ausente/inválida) |
| Regra de domínio | 422 |
| Contrato inexistente | 404 |
| Content-Type errado | 415 |
| Gateway | 401/403/429/504 |
| Falha inesperada | 500 |
| Fail-fast (circuit breaker / load shedding) | 503 + `Retry-After` |

**Nuance do 402.** A RFC 9110 §15.5.3 diz "reserved for future use". Os precedentes divergem: a Stripe usa 402, o **Open Finance Brasil usa 422** para `SALDO_INSUFICIENTE`, a Adyen usa 200 + `Refused`. Mantivemos o pedido. A recusa é registrada e publicada (`TransacaoRecusada`), e nas métricas **recusa de negócio não conta como erro** do SLO.

### 3. Erros

**RFC 9457**, sucessora da RFC 7807 citada no enunciado: mesmo formato e mesmo media type `application/problem+json`. Extensões:
- `codigo` (o "código" estável pedido pelo enunciado);
- `correlation_id`;
- `trace_id`;
- `erros[]` em validação;
- `id_autorizacao` na recusa.

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
  - Três validadores com regras ligeiramente diferentes: gateway (draft-4 parcial), Schemathesis/jsonschema e o serviço. **A autoridade é o serviço.**
  - O 402 é heterodoxo.

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
