# ADR-0001 — Premissas de requisitos não-funcionais que o enunciado não define

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** NFR-01, NFR-02, NFR-03, NFR-04

## Contexto

O enunciado traz volumetria (~80 M contas, 3–5 contratos/conta, pico ~5.000 TPS) e pede para justificar o compute "frente ao SLA de latência", mas não dá latência, disponibilidade, RPO/RTO nem retenção. Sem premissa explícita, nenhuma decisão de compute, persistência ou DR pode ser julgada.

## Decisão: premissas adotadas (revisáveis com o negócio)

| Tema | Premissa | Por quê |
|---|---|---|
| Latência | p99 ≤ 150 ms medido no API Gateway; p99 ≤ 100 ms dentro do serviço | Ordem de grandeza de autorizadores de cartão e do relato público do Itaú (IND3304, re:Invent 2025: < 100 ms, via transcrição) |
| Disponibilidade | SLO de 99,9%/mês (~43 min de error budget) | O SLA do API Gateway é 99,95%. Um SLO igual ao SLA do elo mais fraco deixaria budget zero. 99,99% exigiria ativo-ativo multi-região, e não há consistência forte multi-região a partir de sa-east-1 |
| Região | sa-east-1 primária | Premissa de negócio (latência e postura regulatória). A Res. CMN 4.893/2021 não obriga dado no Brasil, mas exige declarar países e regiões e garantir acesso do BCB (arts. 15–17) |
| DR | Ativo-passivo, desenhado e não implementado | Global tables MREC são last-writer-wins, o que proíbe escrita ativa-ativa no saldo. O DR fora do Brasil reativa os arts. 15–17 |
| RPO / RTO | RPO de segundos (replicação assíncrona); RTO ≤ 30 min com failover manual e fencing de escrita | Consequência da linha anterior |
| Moeda | Só BRL na v1 | O próprio enunciado: "valor da transação em BRL" |
| Retenção | Autorização quente por 30 dias no DynamoDB; trilha regulatória de 10 anos em S3 Object Lock (eventos); reserva expira em 7 dias | Circular BCB 3.978 (registro de operações por 10 anos) |

### Volumetria derivada

| Grandeza | Conta | Valor |
|---|---|---|
| Itens de Limite | 80 M × 3–5 | 240–400 M |
| Requisições em voo | Lei de Little: 5.000 × 50 ms | ~250 |
| WRU no `limites` | 1 `UpdateItem` por operação; medido: 1,09 WRU no DEBITO | ~6 k/s |
| WRU no `autorizacao` | Tabela `autorizacoes`: `PutItem` condicional (1) + `Put` na transação (2), medido 3,06. Tabela `autorizacoes-outbox`, por evento: `Put` na transação de um item de 1–2 KB (4) + delete do relay (2), medido 6,12. A captura publica 2 eventos | `autorizacoes` ~17–27 k/s; `autorizacoes-outbox` ~34 k/s só com DEBITO e ~49–64 k/s com captura, **acima da quota padrão de 40 k por tabela** |
| Eventos | ~5.000 aprovações + ~500 recusas/s; a captura publica 2 eventos | 5,5 k a 10,5 k entradas de PutEvents/s: **a quota aprovada (6.000) não basta**, pedir 12.000 |

## Consequências

- Toda decisão que depende de número (compute, warm throughput, quotas, headroom) aponta para esta tabela, que resume `docs/capacidade.md` §1: as medições e a conta por mix (seções 1.1 e 1.6) estão lá.
- **Medir em vez de supor:** o k6 confere p99 e vazão, e o relatório fica em `docs/capacidade.md`.

## Quando esta decisão deixa de ser boa

Se o negócio fixar SLO de 99,99% ou RPO zero, o desenho muda de classe:

- arquitetura celular;
- consistência forte multi-região (fora de sa-east-1, ou quando a região ganhar suporte);
- ou aceitação explícita de degradação (stand-in) durante falha regional.

## Referências

- API Gateway SLA: https://aws.amazon.com/api-gateway/sla/
- Res. CMN 4.893/2021 (alterada pela CMN 5.274/2025) e Circular 3.978: busca de normativos do BCB, https://www.bcb.gov.br/estabilidadefinanceira/buscanormas
- Pesquisa de suporte: levantamento de quotas da AWS e de normas feito para este desafio (fora do ZIP); as fontes oficiais estão nesta seção
