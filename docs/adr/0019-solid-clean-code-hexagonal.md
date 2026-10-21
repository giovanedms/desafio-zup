# ADR-0019 — SOLID, Clean Code e hexagonal: onde cada princípio está no código

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** ARQ-01, ARQ-02, ARQ-03

## Contexto

O enunciado pede SOLID "de forma explícita e justificada no código", Clean Code e Clean/Hexagonal com inversão de dependência para frameworks e AWS SDK. Princípio só declarado no README não convence numa revisão técnica. Cada um precisa de um lugar no código e, quando dá, de um teste ou gate que quebre se for violado.

## Decisão

**Arquitetura hexagonal por serviço**, com a regra de dependência **verificada na CI** por import-linter (`.importlinter`). Nove contratos:

- em cada serviço, `adapters → application → domain`, e `domain`/`application` **proibidos** de importar `fastapi`, `starlette`, `pydantic`, `boto3`, `botocore`, `httpx` e `uvicorn`;
- no `relay`, `local`/`lambda_handler`/`sweeper` (entradas) → `cdc_limites`/`eventbridge`/`outbox_dynamodb` (adapters) → `nucleo`, e `nucleo` **proibido** de importar `boto3` e `botocore`;
- a `plataforma` (`libs/plataforma`, pacote compartilhado) **proibida** de importar `fastapi`, `starlette`, `pydantic`, `boto3`, `botocore`, `httpx` e `uvicorn`. Como ela está em `root_packages`, o import-linter também a atravessa: um `domain → plataforma.x → boto3` quebra o contrato do serviço.

Um import de `boto3` no núcleo quebra o build, e isso é teste automatizado, não afirmação: `tests/contract/test_gate_de_arquitetura.py` roda o `lint-imports` com este `.importlinter` contra um pacote-fixture (em diretório temporário) com `import boto3` em `autorizacao.domain` e em `relay.nucleo`, e confere que o contrato correspondente fica `BROKEN` e o build sai com 1.

```
services/<svc>/src/<svc>/
├── domain/        regras e invariantes, só stdlib
├── application/   casos de uso + portas (Protocol)
├── adapters/
│   ├── inbound/   HTTP (FastAPI), consumidores
│   └── outbound/  DynamoDB, memória, cliente HTTP do outro BC, CloudEvents
├── config.py      12-factor (ambiente)
└── main.py        composition root: o ÚNICO lugar que conhece classes concretas
```

### Onde cada princípio aparece

| Princípio | Onde | Como verificar |
|---|---|---|
| **SRP** | `limites/domain/limite.py` decide se cabe; `adapters/outbound/dynamodb.py::compilar` traduz para expressão do DynamoDB; `adapters/inbound/http.py` traduz HTTP; `autorizacao/adapters/outbound/cloudevents.py` só formata eventos. `AutorizarTransacao` orquestra e não sabe de HTTP nem de DynamoDB | Cada módulo tem um motivo de mudança. Mudar o formato do evento não toca o domínio |
| **OCP** | `limites/domain/operacoes.py`: cada operação é uma subclasse de `OperacaoDeLimite` que produz um `Plano`. O adapter de persistência compila **o plano**, não o tipo da operação, então uma operação nova não altera nenhum adapter de persistência. O inbound HTTP muda: a operação entra no `Literal` de `OperacaoRequest.tipo` e ganha um `case` em `_para_operacao` (`limites/adapters/inbound/http.py`). Do mesmo jeito, um evento novo no BC Autorização ganha um `case` em `_dados_do_negocio` (`autorizacao/adapters/outbound/cloudevents.py`) | Os testes do repositório rodam qualquer operação pelo mesmo `compilar` |
| **LSP** | `RepositorioDeLimites` tem duas implementações (memória e DynamoDB) e **a mesma suíte de contrato** roda nas duas (`limites/tests/contract/test_porta_repositorio.py`), inclusive a corrida de 100 reservas em 32 threads. Idem `RepositorioDeAutorizacoes` (`autorizacao/tests/contract/`) | `make test` (memória) + `make test-integracao` (DynamoDB na LocalStack) |
| **ISP** | `limites/application/portas.py`: `ConsultaDeLimites`, `AplicacaoDeOperacoes` e `CadastroDeLimites` são portas separadas. `ConsultarLimite` depende só da consulta; `AplicarOperacaoNoLimite`, só da aplicação | mypy strict garante que o caso de uso não chama o que não declarou |
| **DIP** | Casos de uso recebem `Protocol`s (repositório, relógio, gerador de ids, gateway do outro BC). As classes concretas só se encontram em `main.py` | import-linter (`forbidden`) na CI |

### Clean Code — decisões concretas

- **Linguagem ubíqua em português no código** (`Reservar`, `LiberarReserva`, `saldo_reservado`, `Autorizacao.estornar`), a mesma do enunciado e dos contratos. Por isso a regra do ruff que exige sufixo `Error` em exceções está desligada (`pyproject.toml`).
- **Recusa é resultado, não exceção.** `ResultadoDaOperacao(RECUSADA, motivo)`. Exceção fica para o que é anormal (`ContratoNaoEncontrado`, `InvarianteViolada`).
- **Funções curtas com guard clauses** (`Limite.atende`, `Limite.diagnosticar`). A regra de máximo de `return`s subiu para 8 de propósito.
- **Dinheiro sem float em nenhum ponto:** `parse_float=Decimal` na entrada, `Decimal` no domínio, `simplejson(use_decimal=True)` na saída.
- **Testes como especificação:** nomes descrevem o comportamento (`test_reservar_repetido_depois_da_compactacao_continua_idempotente`), um cenário por teste, property-based para invariantes.
- **Gates:** `make check` = ruff (lint + format) + mypy strict + import-linter + pytest. O mypy roda só em `src/` (`Makefile`): os testes ficam fora da checagem de tipos.

## Consequências

- **Positivas:**
  - O domínio é testável em milissegundos, sem AWS.
  - Trocar DynamoDB por Aurora é escrever um adapter e passá-lo na suíte de contrato.
  - O avaliador acha cada princípio pelo nome do arquivo.
- **Negativas:**
  - Mais arquivos e indireção que um CRUD.
  - Para um serviço desse tamanho, alguém pode chamar de cerimônia. A resposta é a suíte de contrato: ela só existe porque há porta.
  - **Dívida registrada: o plumbing HTTP está duplicado, e parte da cópia já divergiu.** Comparando os adapters HTTP do `autorizacao` e do `limites` (`adapters/inbound/http.py`):
    - **idênticos:** `RespostaJsonExata`, `_uuid_canonico`, `_erro_de_campo`, `_dinheiro_json`, `saude`, `parametro_invalido` e, desde o painel 7, o teto da lista de erros do 422 (`MAX_ERROS_POR_PROBLEMA` e `_lista_de_erros`) e a exceção `_NomeRepetido`;
    - **já divergiram:** `_problema` (no `autorizacao` ganhou `origem`, para o replay de uma recusa sair com a correlação gravada), `_Rejeicao` (no `autorizacao` também leva `headers`) e `_objeto_sem_nome_repetido` (a mesma regra contra nome repetido no JSON, com tipos e mensagem diferentes);
    - **o pior caso, o parser de `traceparent`:** o `autorizacao` usa `trace_id_de` da `plataforma` (`fullmatch`), e o `limites` tem o próprio `_trace_id` (`re.match` com `^…$`), que aceita um `\n` no fim do valor que o da `plataforma` recusa. Pelo HTTP/1.1 um valor de header não chega com quebra de linha (RFC 9112, seção 5), então hoje não muda resultado, mas é uma cópia validando por conta própria;
    - **parecidos, sem ser cópia:** `_PROBLEMAS` é um recorte diferente do catálogo em cada serviço, a parte de correlação do middleware faz o mesmo nos dois (UUID canônico ou um novo) e `_saldos_json` tem o mesmo nome, mas monta o schema de saldos de cada contrato.

    Não é autonomia de BC, como a cópia de `dinheiro` (ADR-0015): é infraestrutura HTTP. O destino é `libs/plataforma`, que já é dependência dos dois serviços e hoje tem `Problema`, o contexto de observabilidade (com o parser de `traceparent`) e um pacote `eventos` vazio. Ficou para depois do prazo para não mexer nos dois serviços na reta final. Esse plumbing depende de FastAPI/Starlette, e o contrato `plataforma-pura` do import-linter proíbe framework em `libs/plataforma`: a mudança vem com um submódulo HTTP declarado (dependência no `pyproject` do pacote e o contrato restrito aos submódulos puros), e `domain`/`application` continuam impedidos de importá-lo pelos contratos dos serviços, porque a `plataforma` é `root_package`.

## Quando esta decisão deixa de ser boa

- **Um serviço que vira passa-dados sem regra** (por exemplo um proxy de leitura): camadas demais para pouco domínio. Aí vale um módulo único com o handler chamando o SDK.
- **Contratos de import que ninguém respeita.** Se o gate da CI for desligado, a arquitetura apodrece em silêncio.

## Referências

- Cockburn, A. — Hexagonal Architecture (2005): https://alistair.cockburn.us/hexagonal-architecture/
- Martin, R. C. — The Clean Architecture (2012): https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html
- Liskov, B.; Wing, J. — A Behavioral Notion of Subtyping (1994), ACM TOPLAS 16(6)
- import-linter: https://import-linter.readthedocs.io/
