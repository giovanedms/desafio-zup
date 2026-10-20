# Plataforma de Gestão de Limites e Autorização de Transações

Desafio técnico — Arquiteto de Aplicação (POD99 · Nova Plataforma de Finanças).

Autorização síncrona de transações com **reserva de limite por contrato consistente sob concorrência**, idempotência ponta a ponta, eventos de domínio em CloudEvents via EventBridge, contabilização em partidas dobradas e ACL com o core legado. Em Python 3.13, hexagonal, na AWS (Terraform), com demo local sem AWS.

**Repositório:** https://github.com/giovanedms/desafio-zup. O ZIP da entrega (`make zip`, um `git archive` do HEAD) não leva o histórico: os commits e as execuções da esteira ficam lá.

## Em 3 comandos

Pré-requisitos: Docker, `uv`, `make`, `k6` (opcional). Sem conta nem credencial AWS: o ambiente local roda na LocalStack. Precisa de internet para baixar as imagens e as dependências.

```bash
make setup     # instala dependências (uv)
make up        # LocalStack 4.14.0 (fixada) + recursos + contratos de demo + serviços
make demo      # roteiro ponta a ponta (ver abaixo)
```

| Comando | O que faz |
|---|---|
| `make check` | ruff, mypy strict, regras de arquitetura (import-linter), testes unitários, property-based e de contrato |
| `make test-integracao` | Mesmas suítes contra o DynamoDB da LocalStack (exige `make infra`) |
| `make carga CENARIO=corretude CONTRATO=... LIMITE=...` | k6 |
| `make tf-validate tf-test tf-checkov` | IaC |

**O que a demo mostra:**

1. aprovação (201), replay idempotente (200, corpo idêntico byte a byte) e chave reutilizada (422);
2. **100 reservas paralelas de R$ 100 num limite de R$ 1.000 → exatamente 10 aprovadas e 90 recusadas (402), saldo final zero**;
3. débito, estorno (o segundo estorno da mesma autorização → 409), crédito, contrato bloqueado (422) e contrato inexistente (404);
4. **captura de reserva** (DEBITO que referencia a RESERVA, parcial) e disputa pelo hold: 10 capturas × 10 estornos em paralelo → exatamente 1 vence e os outros 19 recebem 409 (`{201: 1, 409: 19}`), com reservado final zero;
5. erros RFC 9457 e dinheiro exato (`12345678.91` preservado);
6. **fan-out do evento:** o mesmo `TransacaoAutorizada` sai do outbox pelo relay e o EventBridge o entrega nas filas `antifraude` e `notificacoes` e na contabilização, que o lança em partida dobrada;
7. registro do core legado → ACL → limite atualizado: a versão mais antiga que chega depois da mais nova é ignorada e a reserva fica intacta; o `LimiteAlterado` chega na fila `notificacoes`.

**Portas locais:**

| Porta | Serviço |
|---|---|
| `127.0.0.1:18080` | API pública (`autorizacao`) |
| `127.0.0.1:18081` | API interna (`limites`), só para inspeção |
| `127.0.0.1:4566` | LocalStack |

## Onde está cada coisa

```
contracts/          contract-first: OpenAPI 3.0.3 (pública e interna), JSON Schemas dos eventos (CloudEvents), catálogo RFC 9457
services/
  autorizacao/      BC Autorização — API pública, idempotência, decisão, reconciliador de órfãs e expirador de reservas
  limites/          BC Limites — saldo por contrato (escrita condicional atômica), ACL do core legado
  contabilizacao/   BC Contabilização — partidas dobradas, inbox (dedup)
relay/              outbox → EventBridge (Lambda na AWS, container local; mesmo código) + CDC de LimiteAlterado
legado-mock/        simula o core legado (registros de tamanho fixo)
libs/plataforma/    problem+json, contexto de observabilidade
infra/local/        docker compose + bootstrap idempotente
infra/terraform/    módulos + environments sandbox (a stack, sa-east-1) e esteira (OIDC, role de deploy, boundary)
.github/workflows/  esteira: CI em todo push e PR; deploy, smoke e destroy do sandbox a partir da main
tests/              e2e (demo, AWS), load (k6), contract (compatibilidade de schemas), integracao_bcs (os dois BCs reais juntos)
docs/
  adr/              20 decisões (comece pelo README de lá)
  arquitetura/      documento com C4 L1/L2, sequência, eventos, implantação; Well-Architected
  dominio/          linguagem ubíqua
  operacao/         runbooks; infraestrutura.md (inventário AWS, bootstrap, esteira, custo)
  capacidade.md · compliance.md · uso-de-ia.md · cobertura-requisitos.md
```

## Para avaliar rapidamente

1. `docs/arquitetura/documento-de-arquitetura.md`: o mapa, em 10 min.
2. `docs/adr/0005` (concorrência) e `0006` (idempotência): onde está o problema difícil.
3. `services/limites/tests/contract/test_porta_repositorio.py`: a mesma suíte rodando em memória e no DynamoDB, com a corrida de 100 threads.
4. `docs/cobertura-requisitos.md`: cada requisito do enunciado → onde está → nível de evidência.

## AWS

**Pela esteira** (`.github/workflows/esteira.yml`): todo push na `main` passa pelo CI e, verde, faz o deploy no sandbox por OIDC (sem chave de longa duração) e um smoke na AWS real. O destroy é manual (`workflow_dispatch`, `acao=destroy`, com a frase exata).

**Local:** `make deploy EXPECTED_ACCOUNT_ID=<conta> CONFIRMO="CONFIRMO: deploy na conta desafio-zup" [TAG=1.0.4] [ARQ=amd64|arm64]` (pacotes das Lambdas, repositórios ECR, build e push das imagens e apply, nessa ordem; runbook R6).

O que existe na conta (162 recursos do sandbox, ambiente `esteira`, bootstrap), configuração, custo e desvios: `docs/operacao/infraestrutura.md`.

- O deploy exige a frase de confirmação e confere a conta.
- O state fica em S3 com KMS.
- O primeiro apply segue o runbook R6.
- **Custo e cuidado:** janela curta e `make destroy` depois.
