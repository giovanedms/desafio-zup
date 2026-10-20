# Testes de carga (k6)

Script único: `autorizacao.js`. Resultados JSON vão para `tests/load/resultados/`.

## Cenários

| `CENARIO` | O que prova |
|---|---|
| `corretude` | `VUS` (100) requisições simultâneas de RESERVA de `VALOR` (100.00) no mesmo `CONTRATO`: aprovadas (201) têm de ser exatamente `floor(LIMITE/VALOR)`, o resto 402. Sem oversell sob concorrência. Sai com código 99 se violar. |
| `vazao` | `RPS` (50) por `DURACAO` (60s), DEBITO de 1.00 espalhado em `CONTRATOS` (default `CTR-CARGA-0001..0020`). Thresholds: `http_req_failed<1%` e p99 das respostas esperadas (201/402) abaixo de `P99_MS` (150). |

## Local

```bash
make up                                   # semeia CTR-CARGA-0001..0020 (concedido 99999999.00)
# corretude: crie um contrato com limite conhecido (ex.: 10000.00) e informe LIMITE igual
k6 run -e CENARIO=corretude -e CONTRATO=CTR-XXXX -e LIMITE=10000.00 tests/load/autorizacao.js
k6 run -e CENARIO=vazao -e RPS=20 -e DURACAO=10s tests/load/autorizacao.js
```

O p99 local (LocalStack) não representa a AWS; use `P99_MS` para relaxar.

## AWS

```bash
k6 run -e CENARIO=vazao -e BASE_URL=https://<api-id>.execute-api.sa-east-1.amazonaws.com \
  -e API_KEY=... tests/load/autorizacao.js
# ou OAuth2 client_credentials:
#   -e TOKEN_URL=... -e OAUTH_CLIENT_ID=... -e OAUTH_CLIENT_SECRET=...
```

Os contratos `CTR-CARGA-*` precisam existir no ambiente alvo (carga via legado/seed). Segredos só por variável de ambiente, nunca no repositório.

## Variáveis

`BASE_URL`, `CLIENT_ID` (`X-Client-Id`, default `k6`), `API_KEY`, `TOKEN_URL`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `CENARIO`, `CONTRATO`, `LIMITE`, `VALOR`, `VUS`, `CONTRATOS`, `RPS`, `DURACAO`, `P99_MS`.
