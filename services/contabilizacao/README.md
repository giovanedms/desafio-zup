# Serviço contabilizacao
BC Contabilização (suporte). Consome eventos, gera partidas dobradas imutáveis, inbox de deduplicação, ACL de saída para o legado.
Compute: Lambda (SQS) na AWS; poller em container no ambiente local — mesmo núcleo, adapter de entrada diferente.
