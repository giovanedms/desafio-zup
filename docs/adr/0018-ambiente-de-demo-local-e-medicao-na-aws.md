# ADR-0018 — Demo local offline (LocalStack 4.14.0 fixada) e medição de desempenho na AWS real

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** DEF-03

## Contexto

A defesa exige demonstrar a solução executando, "local com docker ou em conta AWS".

Fatos (`01-pesquisa/02` §11):

- **A imagem `localstack/latest` exige conta e token** desde 23/03/2026, e o repositório foi arquivado.
- **A tag `4.14.0`** (último build Community, 26/02/2026) sobe **sem token e sem rede**, com DynamoDB, Streams, SQS, EventBridge e STS.
- **O DynamoDB Local não simula `TransactionConflictException`.**
- **Medido nesta máquina:** o DynamoDB da LocalStack faz **~49 UpdateItem/s com 1 thread e ~67/s com 20 threads**. Ele praticamente não escala, o que limita a vazão local a ~13 autorizações/s.

## Decisão

1. **Demo principal 100% local e offline:**
   - **Containers:** LocalStack `4.14.0` **fixada por digest** + `autorizacao`, `limites`, `relay`, `contabilizacao`, consumidor da ACL e reconciliador.
   - **Bootstrap idempotente**, que usa as mesmas definições de tabela dos testes.
   - **Credenciais fictícias explícitas** em todos os containers.
   - **Comandos:** `make up` e `make demo`.
2. **Na demo local vale corretude, não desempenho:** corrida de 100 reservas, idempotência, operações, erros, fluxo de eventos até a contabilização, ACL do legado e reconciliador.
   - **Funciona igual na AWS** porque nenhuma escrita no item quente usa transação (ADR-0005): o emulador e a AWS se comportam igual no que importa.
   - **A latência local é explicitamente marcada como "não é referência"**, com o dado medido acima.
3. **Desempenho medido na AWS real**, numa janela curta (apply → medição → gravação → destroy). Feito em 20/10/2026:
   - k6 disparado da máquina do operador contra o API Gateway em sa-east-1. A latência do k6 inclui a internet; a de servidor vem das métricas do gateway (`Latency`/`IntegrationLatency`), por isso não foi preciso rodar o k6 dentro da região;
   - a mesma corrida de concorrência contra o DynamoDB real (`tests/e2e/aws.py corrida`);
   - degraus de 150 a 600 rps com CPU por serviço (Container Insights), latência por salto e WRU por tabela, relatório em `docs/capacidade.md`. Sem vídeo: a saída da demo local fica gravada como plano B.
4. **Plano B se a imagem fixada falhar:** `amazon/dynamodb-local` + ElasticMQ, com os mesmos adapters (só muda o endpoint).

## Consequências

- **Positivas:**
  - A demo não depende de internet, conta nem licença no dia.
  - A afirmação de corretude vale nos dois ambientes.
  - O número de desempenho vem do ambiente certo.
- **Negativas:**
  - **A imagem fixada está congelada** (sem patches). Mitigação: só roda localmente, em rede interna, e nunca vai para produção.
  - **O API Gateway não é emulado localmente:** Cognito, cotas e erros do gateway aparecem na janela AWS.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| A avaliação exigir desempenho ao vivo | Demo direto na conta AWS (o Terraform existe), com a janela aberta no dia |
| A LocalStack 4.14 parar de funcionar com o Docker atual | Plano B acima |

## Referências

- LocalStack licensing FAQ: https://docs.localstack.cloud/getting-started/faq
- DynamoDB Local: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.html
