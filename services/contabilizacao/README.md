# Serviço contabilizacao
BC Contabilização (suporte). Consome eventos, gera partidas dobradas imutáveis, inbox de deduplicação. A ACL de saída para o legado (exportação posicional) está desenhada e não implementada (ADR-0014, item 7).
Compute: Lambda (SQS) na AWS; poller em container no ambiente local — mesmo núcleo, adapter de entrada diferente.
