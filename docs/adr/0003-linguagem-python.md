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
  - **Menor vazão por vCPU que a JVM.** Mitigação: o caminho é I/O-bound (DynamoDB e salto HTTP), o escalonamento é horizontal no Fargate, e o k6 mede a vazão real por task.
  - **GIL.** Mitigação: endpoints delegam o I/O bloqueante ao threadpool; um processo por container, várias tasks.
  - **Fora da stack padrão do banco.** Contraponto: o contrato (OpenAPI e eventos) é a fronteira, e um serviço Java poderia substituir qualquer um destes passando nas mesmas suítes de contrato.

## Quando esta decisão deixa de ser boa

- **O k6 mostra que o custo por TPS** (tasks Fargate necessárias) supera um limiar acordado com FinOps: migra-se o serviço quente (`limites`) para a JVM ou para Go, mantendo o contrato.
- **Plataforma do banco** exige paved road JVM, com observabilidade, segurança e bibliotecas internas só para Java.

## Alternativas descartadas

| Opção | Por que não agora |
|---|---|
| Java 21 + Spring Boot 3 | Sinal de "casa" mais forte, mas a defesa de código gerado numa linguagem menos praticada pelo operador é o maior risco do desafio. O exemplo público de solução Java para este mesmo enunciado nunca compilou |
| Kotlin | Mesmo raciocínio, com ainda menos familiaridade |
| Implementação dupla (Python + Java) | Avaliada; cortada pelo prazo, porque dobraria a superfície de perguntas sem acrescentar decisão de arquitetura |

## Referências

- mypy strict: https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict
- FastAPI e threadpool para código bloqueante: https://fastapi.tiangolo.com/async/
