# ADR-0015 — Padrões táticos: Repository e Domain Events sim; CQRS leve; Event Sourcing não

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** ARQ-05

## Contexto

O enunciado pede para "discutir uso (ou não) de CQRS, Repository, Domain Events, e justificar os trade-offs".

## Decisão

### Repository: **sim, em duas formas, de propósito**

- **Clássico** (`RepositorioDeAutorizacoes`): carrega o agregado, o domínio muda o estado e o repositório grava **condicionado** (lease e status).
  - Serve para Autorização porque **não há contenção**: cada autorização é um item.
- **"Repositório de comando"** (`RepositorioDeLimites.aplicar(operacao)`): o domínio produz um `Plano` declarativo (pré-condições e efeitos) e o adapter o **compila numa escrita condicional atômica**, sem ler antes.
  - Serve para Limite porque o item é **quente**. Ler, modificar e gravar com versão causaria conflito em cascata (ADR-0005).
  - O domínio continua dono da regra: `Limite.atende` e `Limite.diagnosticar` são a mesma lógica nos dois adapters, provada pela mesma suíte de contrato.

### Domain Events: **sim**

- Transições do agregado Autorização registram eventos (`TransacaoAutorizada`, `TransacaoRecusada`, `TransacaoEstornada`, `ReservaExpirada`).
- O repositório os grava no **outbox na mesma escrita** do estado (ADR-0007).
- O agregado não publica nada; ele declara fatos.
- `LimiteAlterado` sai por CDC (ADR-0014).
- O termo "Domain Events" não está no livro original de Evans (2003): é da DDD Reference (2015) e de Vernon (2013).

### CQRS: **leve**

- **Sem modelo de leitura separado no caminho quente.** As leituras são por chave (GET da autorização, retrato do limite) e servidas pelo próprio modelo de escrita com leitura consistente. Um segundo modelo traria defasagem e um sincronizador para manter, sem ganho para esse acesso.
- **A separação existe onde há ganho:** os eventos alimentam modelos de leitura de outros contextos (lançamentos da contabilização, data platform, antifraude). É CQRS **entre** bounded contexts, não dentro de um.

### Event Sourcing: **não**

- **A trilha de auditoria pedida já sai dos eventos imutáveis**, publicados e arquivados (10 anos em S3 Object Lock na produção).
- Event sourcing no item quente agravaria a contenção: cada reserva viraria um append sequenciado por contrato.
- E traria snapshot, upcasting de eventos e reconstrução, sem requisito que o pague.

## Consequências

- **Positivas:** cada padrão aparece onde resolve um problema concreto, e cada não-uso tem motivo.
- **Negativas:** duas formas de Repository no mesmo sistema exigem explicação. É exatamente o ponto: o padrão segue a forma de acesso.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Consultas ricas (extrato de autorizações por conta e período, com filtros) | Modelo de leitura dedicado (DynamoDB com GSI por conta e data ou OpenSearch), alimentado pelos eventos. Aí entra CQRS dentro do BC |
| Exigência de reconstruir o saldo de qualquer instante passado ("saldo em D-3 às 14h") | Event sourcing do Limite, com ledger append-only particionado por contrato, ou um ledger dedicado. Reavaliar contenção e custo |
| Mais BCs precisarem do mesmo conceito de dinheiro | Mover `dinheiro` para uma lib (**Shared Kernel**). Hoje cada BC tem a sua cópia, por autonomia |

## Referências

- Fowler — CQRS (e quando não usar): https://martinfowler.com/bliki/CQRS.html
- Fowler — Repository: https://martinfowler.com/eaaCatalog/repository.html
- Fowler — Event Sourcing: https://martinfowler.com/eaaDev/EventSourcing.html
- Vernon — Effective Aggregate Design: https://www.dddcommunity.org/library/vernon_2011/
