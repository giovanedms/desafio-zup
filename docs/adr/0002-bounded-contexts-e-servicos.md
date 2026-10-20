# ADR-0002 — Três bounded contexts, três serviços; Limites separado de Autorização

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-01, RN-07, ARQ-04, MS-01, API-15

## Contexto

O enunciado sugere "Serviço de Limites, Serviço de Autorização, Serviço de Contabilização" e cita os agregados Contrato, Limite e Autorização. A pergunta de design que importa: **se a reserva de limite precisa ser atômica com a decisão, por que dois serviços?**

## Decisão

| Bounded context | Tipo | Agregado(s) | Serviço | Responsabilidade |
|---|---|---|---|---|
| Autorização de Transações | Core | Autorização | `autorizacao` | API pública, idempotência, decisão, ciclo de vida (aprovar, recusar, estornar, expirar), eventos |
| Gestão de Limites | Core | Limite | `limites` | Saldo em tempo real por contrato, reservas, invariantes, ACL de entrada do legado |
| Contabilização | Suporte | Lançamento | `contabilizacao` | Partidas dobradas a partir dos eventos, ACL de saída para o legado |
| Contratos (legado) | Upstream | Contrato | core banking | Fonte da verdade de contrato, status e limite concedido |

**Context map:**

| Relação | Padrão |
|---|---|
| Canais → Autorização | **Open Host Service + Published Language** (REST v1 OpenAPI) |
| Autorização → Limites | **Customer/Supplier** (API interna de operações) |
| Legado → Limites | **Anticorruption Layer** assíncrona |
| Autorização → Contabilização | **Published Language** (CloudEvents) |

**Contrato** é agregado do contexto de contratos, que hoje é o legado. O BC Limites guarda só uma réplica (status, produto, conta) e **não** é dono do contrato. Os tipos de contrato (crédito, investimento, seguro, consórcio — RN-07) aparecem como `tipo_produto` na réplica.

### Por que Limites é um serviço separado

1. **Motivos de mudança diferentes.** A política de crédito e o ciclo do limite (concessão, bloqueio, reajuste via core) mudam por razões diferentes das regras de autorização por canal.
2. **Mais de um consumidor.** No banco real, o limite é consumido por cartão, Pix e crédito direto. A API interna de operações é o contrato estável entre eles.
3. **Dono e escala diferentes.** O item quente é o contrato, e o limite escala por contrato, não por canal.

### Por que a atomicidade não exige um serviço só

**Fundir não elimina o problema.** A reserva é um `UpdateItem` atômico no item do contrato (ADR-0005), e a decisão é registrada depois, no item da autorização. Mesmo num serviço só seriam duas escritas, porque juntá-las numa transação reintroduziria a contenção no item quente.

A consistência entre os dois vem de:

- idempotência pelo mesmo id (`id_operacao = id_autorizacao`);
- retomada por lease;
- reconciliador que compensa órfãs (ADR-0006).

O custo do salto HTTP interno entre serviços **não foi isolado** nas medições (estimativa de 2–5 ms). O que foi medido na AWS é a latência por salto do DynamoDB: `UpdateItem` ~4 ms, `PutItem` ~3,6 ms e `TransactWriteItems` ~21 ms (`docs/capacidade.md`, seção 3).

## Consequências

- **Positivas:** fronteiras claras, contratos versionados entre BCs e prova de LSP via suíte de contrato em cada porta.
- **Negativas:** um salto síncrono a mais e consistência por compensação em vez de transação local.

## Quando esta decisão deixa de ser boa

- **Fundiria se** o `limites` tiver um único consumidor (esta API) **e** o salto aparecer no p99, ou se o mesmo time for dono dos dois e o deploy independente não trouxer ganho.
- **Separaria a ACL num serviço próprio se** o volume e a variedade de formatos do legado crescerem a ponto de ter ciclo de deploy próprio.

## Referências

- Evans, E. — Domain-Driven Design Reference (2015): https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf
- Vernon, V. — Effective Aggregate Design (2011): https://www.dddcommunity.org/library/vernon_2011/
