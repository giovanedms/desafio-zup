# Testes de carga (k6)

Script único: `autorizacao.js`. Resultados JSON vão para `tests/load/resultados/`.

## Cenários

| `CENARIO` | O que prova |
|---|---|
| `corretude` | `VUS` (100) requisições simultâneas de RESERVA de `VALOR` (100.00) no mesmo `CONTRATO`: aprovadas (201) têm de ser exatamente `floor(LIMITE/VALOR)`, o resto 402. Sem oversell sob concorrência. Sai com código 99 se violar. |
| `vazao` | `RPS` (50) por `DURACAO` (60s), DEBITO de 1.00 num contrato sorteado a cada requisição: da lista `CONTRATOS`, se houver, ou entre `N_CONTRATOS` contratos `PREFIXO` + número com `PAD` dígitos (default `CTR-CARGA-0001..0020`). Thresholds: `http_req_failed<1%` e p99 das respostas esperadas (201/402) abaixo de `P99_MS` (150). |
| `pico` | Mesma requisição do `vazao`, em rampa (`ramping-arrival-rate`): de `RPS`/10 até `RPS` em `RAMPA` (3m) e patamar de `DURACAO` em `RPS`. Mesmos thresholds. Foi o cenário do teste de 5.000 rps de 21/10 (`docs/capacidade.md` §3.1). |

## Local

```bash
make up                                   # semeia CTR-CARGA-0001..0020 (concedido 99999999.00)
# corretude: crie um contrato com limite conhecido (ex.: 10000.00) e informe LIMITE igual
k6 run -e CENARIO=corretude -e CONTRATO=CTR-XXXX -e LIMITE=10000.00 tests/load/autorizacao.js
k6 run -e CENARIO=vazao -e RPS=20 -e DURACAO=10s tests/load/autorizacao.js
```

O p99 local (LocalStack) não representa a AWS; use `P99_MS` para relaxar.

**Gerador e contratos.** O contrato é sorteado pelo número, sem montar a lista: com 100 mil contratos e milhares de VUs, uma lista por VU esgota a memória do gerador. A conta enviada sai do contrato (`CTR-DEMO-n` e `CTR-CARGA-n` com as regras de quem os cria; qualquer outro prefixo, `0001-77777777-7`), ou de `ID_CONTA`. `VUS_PRE`/`VUS_MAX` substituem o padrão de 1 VU pré-alocado por rps (máximo 4 por rps), que a 5.000 rps só gasta memória.

## AWS

```bash
k6 run -e CENARIO=vazao -e BASE_URL=https://<api-id>.execute-api.sa-east-1.amazonaws.com \
  -e API_KEY=... tests/load/autorizacao.js
# ou OAuth2 client_credentials:
#   -e TOKEN_URL=... -e OAUTH_CLIENT_ID=... -e OAUTH_CLIENT_SECRET=...
```

Os contratos precisam existir no ambiente alvo, na conta que o k6 manda: `tests/e2e/aws.py contratos [N] [PREFIXO]` os cria pelo legado (fila → `limites-acl`), sempre na conta `0001-77777777-7`: use um prefixo que não seja `CTR-CARGA-` (ex.: `CTR-K-`, com `PAD=6`), porque para `CTR-CARGA-n` o k6 manda a conta do bootstrap local e o `limites` recusa com `CONTA_DIVERGENTE`. Segredos só por variável de ambiente, nunca no repositório.

```bash
# Degrau curto com 5.000 contratos CTR-K-000001..005000 (segredos lidos do terraform output, nunca impressos)
AWS_PROFILE=desafio-zup uv run python tests/load/aws_k6.py RPS=150 DURACAO=60s N_CONTRATOS=5000 PREFIXO=CTR-K- PAD=6
```

**5.000 rps: o gerador dentro da região.** O teste de 21/10 rodou o k6 v0.55.0 (imagem `grafana/k6:0.55.0`) numa task Fargate de 8 vCPU / 16 GB em sa-east-1, na VPC do sandbox, com IP público e security group só com saída 443, criada e apagada fora do Terraform (o script que faz isso não está no repositório). Assim a latência do k6 não inclui a internet até a região, e a vazão oferecida não depende da máquina nem da conexão de quem dispara. Parâmetros do teste: `CENARIO=pico RPS=5000 RAMPA=3m DURACAO=8m N_CONTRATOS=100000 PREFIXO=CTR-K- PAD=6 VUS_PRE=2000 VUS_MAX=8000 P99_MS=1000`, contra os 100.000 contratos `CTR-K-*` criados direto no DynamoDB pelo caso de uso da ACL. Para repetir: subir `plano_carga` e os dois tetos do WAF, desligar a regra da data platform (`plataforma_de_dados_ativa = false`; em 21/10 ela ainda não existia) e ter quota de Fargate para as tasks e o gerador (`terraform.tfvars.example`, runbook R6). Resultado e números: `resultados/pico-2026-10-21T23-18-14Z.json` e `docs/capacidade.md` §3.1.

## Variáveis

`BASE_URL`, `CLIENT_ID` (`X-Client-Id`, default `k6`), `API_KEY`, `TOKEN_URL`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `CENARIO` (`corretude`, `vazao` ou `pico`), `CONTRATO`, `LIMITE`, `VALOR`, `VUS`, `CONTRATOS`, `N_CONTRATOS` (20), `PREFIXO` (`CTR-CARGA-`), `PAD` (4), `ID_CONTA`, `RPS`, `DURACAO`, `RAMPA` (3m, só no `pico`), `VUS_PRE` e `VUS_MAX` (0 = o padrão por rps), `P99_MS`.
