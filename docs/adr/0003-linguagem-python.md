# ADR-0003 — Python 3.13 nos três serviços

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** IA-03 (e viabiliza ARQ-01..03)

## Contexto

O enunciado não exige linguagem. A stack predominante do Itaú e da Zup é Java/Kotlin. O §4 do enunciado avalia "entendimento do código gerado", com perguntas específicas na defesa. O código foi produzido com apoio de IA (ver `docs/uso-de-ia.md`).

## Drivers

1. **Defender cada linha.** É o critério que o próprio enunciado põe duas vezes.
2. **Expressar o domínio com clareza:** dinheiro exato, imutabilidade, tipos.
3. **Vazão suficiente** para 5.000 TPS com o compute escolhido (ADR-0004).

## Decisão

**Python 3.13**, com tipagem estrita (mypy strict) e:

- **FastAPI** nos adapters HTTP;
- **boto3** nos adapters AWS;
- **Hypothesis** para testes de propriedade.

Domínio e aplicação **não importam framework nem SDK** (import-linter na CI, ADR-0019).

**Como fica em Java/Kotlin:** o desenho não muda.

| Python | Java / Kotlin |
|---|---|
| `Protocol` | `interface` |
| `@dataclass(frozen=True)` | `record` / `data class` |
| `Decimal` | `BigDecimal` |
| FastAPI | Spring WebMVC ou WebFlux |
| boto3 | AWS SDK v2 com o mesmo `UpdateItem` condicional |
| Suíte de contrato | Mesma suíte, via JUnit parametrizado |

## Consequências

- **Positivas:**
  - Domínio enxuto e legível.
  - Toolchain rápido (uv, ruff).
  - Testes de propriedade naturais.
  - O operador domina a linguagem, então a defesa é sobre decisões e não sobre sintaxe.
- **Negativas:**
  - **No degrau medido, o caminho síncrono é CPU-bound, não I/O-bound.** Na AWS (`docs/capacidade.md` §3), o `autorizacao` saturou a **97% de CPU com 243 rps** em 4 tasks de 1 vCPU (versão 1.0.2). Na 1.0.3 a autorização custa **~8 ms de CPU** (~122 rps por vCPU a 100%) e a operação no `limites`, ~4,6 ms. O I/O pesa na latência (a `TransactWriteItems` de ~21 ms é o maior custo fixo do caminho), mas o que satura a task é o Python.
  - **Como se paga: escala horizontal.** Os serviços não guardam estado de negócio na task, então vazão é número de tasks Fargate: para 5.000 TPS, ~106 vCPU com CPU a 60% e ~200 vCPU para segurar o p99 de gateway perto de 165 ms (`docs/capacidade.md` §3). O teste de 5.000 rps de 21/10 confirmou a forma e apertou o número: com ~10 ms de CPU por requisição em cada serviço (derivado), 120 vCPU a ~85% aceitaram 96,2%, e aceitar tudo pede ~150–160 vCPU (§3.1).
  - **O trade-off Python × JVM/Go, explícito.** Um runtime compilado faria o mesmo trabalho com menos CPU por requisição; quanto menos, aqui, não foi medido. Ficar no Python custa:
    - **mais vCPU**, na parcela menor da conta: no pico, ~US$ 9–17/h de Fargate contra ~US$ 300–526/h de escrita no DynamoDB (`docs/capacidade.md` §4);
    - **parte do p99:** o RNF de p99 ≤ 150 ms não foi atingido em nenhum degrau (o mais perto: 164 ms no gateway, a 33% de CPU), e a distância é Python mais a transação de ~21 ms. A primeira alavanca tira a transação do caminho comum e não depende da linguagem (`docs/capacidade.md` §5, alavanca 1).

    O que se compra em troca é o driver 1: defender cada linha, o maior risco deste desafio.
  - **GIL.** Um processo Python executa bytecode em um núcleo por vez. Por isso há um processo por container, a task foi medida com 1 vCPU e a unidade de escala é a task; os endpoints delegam o I/O bloqueante (boto3) ao threadpool.
  - **Fora da stack padrão do banco.** Contraponto: o contrato (OpenAPI e eventos) é a fronteira, e um serviço Java poderia substituir qualquer um destes passando nas mesmas suítes de contrato.

## Quando esta decisão deixa de ser boa

- **O custo por TPS** (vCPU de Fargate, medida pelo k6) supera um limiar acordado com FinOps, **ou o p99 ≤ 150 ms segue fora depois da alavanca 1** (`docs/capacidade.md` §5): migram-se para a JVM ou para Go os dois serviços do caminho síncrono, a começar pelo que mais gasta CPU por requisição (hoje o `autorizacao`, ~8 ms contra ~4,6 ms do `limites`), mantendo o contrato.
- **Plataforma do banco** exige paved road JVM, com observabilidade, segurança e bibliotecas internas só para Java.

## Alternativas descartadas

| Opção | Por que não agora |
|---|---|
| Java 21 + Spring Boot 3 | Sinal de "casa" mais forte, mas a defesa de código gerado numa linguagem menos praticada pelo operador é o maior risco do desafio |
| Kotlin | Mesmo raciocínio, com ainda menos familiaridade |
| Implementação dupla (Python + Java) | Avaliada; cortada pelo prazo, porque dobraria a superfície de perguntas sem acrescentar decisão de arquitetura |

## Referências

- mypy strict: https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict
- FastAPI e threadpool para código bloqueante: https://fastapi.tiangolo.com/async/
