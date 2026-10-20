# ADR-0017 — Observabilidade, trilha de auditoria, segurança e compliance

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-06, PAP-01 (pilares de segurança e excelência operacional)

## Contexto

O caso é regulado: o banco é S1 e segue as Resoluções CMN 4.893/2021 (alterada pela 5.274/2025), BCB 85/2021, a Circular 3.978 e a LGPD. O enunciado pede "auditoria, rastreabilidade e idempotência de operações financeiras". Detalhe por norma em `docs/compliance.md`.

## Decisão

### Rastreabilidade ponta a ponta

- **Três identificadores, papéis diferentes:**
  - `correlation_id`: negócio, nasce no canal ou no serviço;
  - `traceparent` (W3C): técnico;
  - `id_autorizacao`: entidade.
- **Os três atravessam:** HTTP → serviço → chamada ao `limites` → item gravado → CloudEvent (extensões `traceparent` e `correlationid`) → fila → consumidor → lançamento.
- O `X-Correlation-Id` recebido só é aceito se for UUID canônico; senão é gerado, o que protege contra log injection.
- **Na AWS:** X-Ray no API Gateway, access log JSON com `requestId`, `extendedRequestId` e latências, logs estruturados nos serviços. A instrumentação OpenTelemetry (ADOT) fica como evolução.
- **Junção borda × serviço:** a integração do gateway injeta `X-Gateway-Request-Id` = `$context.requestId`, o mesmo valor da linha do access log e do `correlation_id` das gateway responses, sobrescrevendo qualquer valor do cliente. A AWS avisa que o cliente pode influenciar o `requestId`; por isso o access log grava também o `extendedRequestId`, único e gerado pelo gateway, para desempatar.

### Trilha de auditoria

- **Todo fato de negócio vira evento imutável**, inclusive a **recusa** (`TransacaoRecusada`).
- **Produção:** archive do EventBridge e Firehose → **S3 com Object Lock (compliance mode) por 10 anos** (Circular 3.978, arts. 28/67).
- **Sandbox:** archive de 30 dias e CloudTrail da conta num bucket com Object Lock, que **demonstra** o mecanismo WORM. O trail (`desafio-zup-trail`) e o bucket (`desafio-zup-cloudtrail-<conta>`, Object Lock GOVERNANCE de 1 dia, expiração em 30 d) foram criados no bootstrap da conta, **fora do Terraform deste repositório**.
- **Imutabilidade contábil:** correção é estorno (outro lançamento), nunca edição.

### Métricas e SLOs

- **RED por rota** (taxa, erros, duração). **Recusa (402) não conta como erro de SLO.**
- **Métricas de negócio:**
  - taxa de aprovação;
  - recusas por motivo;
  - idade da mensagem mais antiga por fila;
  - tamanho do outbox;
  - IteratorAge do relay;
  - órfãs compensadas por hora.
- **Alarmes:** 5XX > 1%, p99 > 150 ms, qualquer mensagem em DLQ (inclusive a DLQ de entrega dos targets do EventBridge), IteratorAge > 60 s, guarda de idempotência acima de 2.000 entradas (`OperacoesNaGuarda`) e reservas ativas no item acima de 2.000 (`ReservasAtivas`, ADR-0016), defasagem do feed do legado acima de 5 min (ADR-0014) e os do reconciliador (`reconciliador-encaminhadas`, `reconciliador-falhas`, `reconciliador-hold_perdido`). Órfãs compensadas por hora é consulta de log (runbook R4), sem alarme.
- **Ação dos alarmes:** todos notificam (ALARM e OK) o tópico SNS `desafio-autorizacao-alarmes`, criptografado com chave KMS gerenciada pelo cliente e rotação ligada. Não é a `alias/aws/sns`: com ela o CloudWatch não consegue publicar. A policy do tópico só aceita o CloudWatch vindo de alarmes da conta (`aws:SourceAccount`, `aws:SourceArn`). No sandbox não há assinatura por padrão (`var.email_alarmes`); na produção o plantão assina.

### Segurança

- **IAM:** uma task role por serviço, ações mínimas e recursos por ARN, verificado por `terraform test` e Checkov na CI. As trust policies das task roles e da role do EventBridge Scheduler têm `aws:SourceAccount` e `aws:SourceArn` (anti-confused-deputy), só onde a AWS documenta o suporte (detalhe em `docs/operacao/infraestrutura.md`, seção 4.9).
- **Rede:**
  - tasks sem ingress da internet (só NLB e Service Connect, a partir da VPC);
  - na produção, subnets privadas + endpoints;
  - no sandbox, subnets públicas **sem ingress** e sem NAT, por custo, com VPC endpoints gateway só para DynamoDB e S3 (desvio documentado).
- **Borda:** WAF (rate-based + regras gerenciadas), OAuth2 com escopo, cotas.
- **Dados:**
  - DynamoDB com criptografia em repouso e PITR, SQS com SSE, TLS em trânsito;
  - **nenhuma PII em eventos, logs ou traces:** só identificadores pseudonimizados (`id_conta`, `id_contrato`);
  - `metadata` com allowlist sem geo preciso.
- **Segredos:** nenhum no código ou na imagem. O state do Terraform fica em S3 com KMS, e o `.gitignore` bloqueia `*.tfstate*`.
- **Supply chain:** dependências travadas (`uv.lock`), imagens com usuário não-root, tags imutáveis no ECR, Trivy na CI.

## Consequências

- **Positivas:**
  - Cada requisito regulatório aponta um mecanismo concreto e verificável.
  - A observabilidade separa erro técnico de recusa de negócio.
- **Negativas:**
  - **Custo de armazenamento** da trilha de 10 anos (mitigado por classes frias do S3).
  - **Sem OpenTelemetry completo no prazo:** o trace técnico depende do X-Ray na borda e dos IDs propagados.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Ferramenta corporativa de observabilidade (Datadog é citado nas vagas) | Exportar via OTel Collector, sem mudar a instrumentação de IDs |
| Norma nova exigindo assinatura de eventos | Assinatura do CloudEvent no outbox (extensão), com verificação nos consumidores |

## Referências

- W3C Trace Context: https://www.w3.org/TR/trace-context/
- S3 Object Lock: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html
- Normas BCB (busca de normativos): https://www.bcb.gov.br/estabilidadefinanceira/buscanormas · LGPD: https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm
