# Operação
`slo.md`: SLIs, o que conta como falha (inclusive o 503 de load shedding), SLO de 99,9%/mês e error budget; a política de burn-rate em duas janelas está desenhada, não implementada.
`runbooks.md`: R1–R9 (DLQ/replay de eventos, outbox, aumento de quota e warm throughput pré-pico, órfãs, failover regional desenhado, deploy, reconciliação manual, hold e contrato quente, defasagem do legado). Matriz: PAP-01, RN-04.
`infraestrutura.md`: o que existe na conta AWS (inventário dos 174 recursos do sandbox, ambiente `esteira`, bootstrap), esteira, custo e desvios do sandbox.
