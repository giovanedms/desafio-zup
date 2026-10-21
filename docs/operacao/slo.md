# SLOs e error budget

As premissas estão no ADR-0001 (99,9%/mês; p99 ≤ 150 ms no gateway). Aqui fica o que se mede, o que conta como falha e como alarmar. Escopo: a API pública (`autorizacao`), medida no API Gateway, stage `v1`.

## SLIs

| SLI | Definição | Fonte |
|---|---|---|
| Disponibilidade | Requisições sem erro do servidor ÷ requisições | `5XXError` e `Count` do API Gateway |
| Latência | Requisições sem erro do servidor respondidas em até 150 ms ÷ requisições sem erro do servidor | `Latency` do API Gateway (inclui a integração) |

## O que conta como falha

| Resposta | No SLI de disponibilidade | Por quê |
|---|---|---|
| 2xx e 402 | Sucesso | A recusa de negócio é resultado, não erro (ADR-0011, ADR-0017) |
| Demais 4xx | Sucesso | Erro do cliente, respondido como o contrato manda |
| 429 (usage plan ou WAF) | Sucesso | O consumidor passou da cota ou do teto dele. O 429 do throttle **da conta** seria falha de capacidade, mas o gateway usa a mesma resposta (`THROTTLED`) para conta e plano; a ~50% do throttle ele não deve aparecer, e se aparecer é runbook R3 |
| 500, 503, 504 | **Falha** | Inclusive o **503 de load shedding** e o do disjuntor |

**Por que o 503 de shedding conta.** É o jeito certo de falhar (rápido, com `Retry-After`, sem corromper nada: ADR-0013), mas a requisição não foi atendida por falta de capacidade. Contar no budget é o que liga o budget ao dimensionamento: shedding frequente quer dizer que a folga de 1,6× (ADR-0004) está errada. O `5XXError` do gateway já soma os 503, sem separar o motivo, e isso basta para o SLI.

## SLO e error budget

- **Disponibilidade: 99,9% por mês** (janela móvel de 30 dias). Budget: 0,1% das requisições, ~43 min de indisponibilidade total.
- **Latência: 99% das requisições sem erro em até 150 ms** no gateway, o p99 do ADR-0001. **Alvo não atingido:** o melhor degrau medido teve p99 de 164 ms no gateway (150 rps, 33% de CPU); 526 ms a 300 rps; 874 ms a 600 rps (`docs/capacidade.md`, seção 3). O caminho está na seção 5 do mesmo documento.
- **Ordem de grandeza:** o degrau de 600 rps com 6 tasks teve 4,4% de 503, 44× a taxa do budget. Sustentado, gastaria o mês em ~16 h.

## Política de burn-rate (desenhada, não implementada)

Taxa de queima = taxa de 5xx ÷ 0,1%. São dois alertas, cada um com janela longa e janela curta, e as duas precisam estar acima do limiar: a longa dá significância, a curta faz o alerta parar logo depois da recuperação (Google SRE Workbook, "Alerting on SLOs").

| Alerta | Queima | Janela longa | Janela curta | Taxa de 5xx que dispara | Budget gasto ao disparar | Ação |
|---|---|---|---|---|---|---|
| Rápido | 14,4× | 1 h | 5 min | > 1,44% | 2% do mês | Página ao plantão |
| Lento | 6× | 6 h | 30 min | > 0,6% | 5% do mês | Página ao plantão |

- **Implementação prevista:** um alarme de metric math (`5XXError` ÷ `Count`) por janela, os dois de cada alerta combinados num alarme composto (AND), com ação no tópico SNS de alarmes (ADR-0017).
- **Budget esgotado no mês:** só entram mudanças de confiabilidade e capacidade até a janela de 30 dias voltar ao positivo.

## Alarmes de API que a IaC tem hoje

`infra/terraform/modules/observabilidade/main.tf` tem dois, e nenhum é de burn-rate:

| Alarme | Regra | O que ele não cobre deste SLO |
|---|---|---|
| `api_5xx` | 5xx ÷ `Count` > 1% num período de 5 min | Janela única; 1% é 10× a taxa do budget: 0,9% sustentado não alarma e gasta o mês em ~3 dias. Conta o 503 de shedding, o que está certo por este SLI |
| `api_p99` | p99 do `Latency` > 150 ms em 3 de 5 min | O p99 medido passou de 150 ms em todos os degraus: sob carga sustentada ele dispara e mostra o alvo não atingido, não um incidente. Não tem runbook |

O SLO cobre a API síncrona. O caminho assíncrono não tem SLO definido; os sinais dele são os alarmes de DLQ, IteratorAge e defasagem do legado (ADR-0017).

## Referências

- Google SRE Workbook, "Alerting on SLOs" (burn rate, múltiplas janelas): https://sre.google/workbook/alerting-on-slos/
- API Gateway, tipos de gateway response (`THROTTLED` cobre usage plan, método, stage e conta): https://docs.aws.amazon.com/apigateway/latest/developerguide/supported-gateway-response-types.html
- CloudWatch, alarmes compostos: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Create_Composite_Alarm.html
