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
- **No caminho da autorização, os três vão até a fila:** HTTP → `autorizacao` → item da autorização (grava `correlation_id` e `traceparent`) → CloudEvent no outbox (extensões `correlationid` e `traceparent`; `correlation_id`, `trace_id` e `id_autorizacao` no `data`) → fila. A chamada ao `limites` leva `X-Correlation-Id` e `traceparent` nos headers, e o `id_operacao` é o `id_autorizacao` ou um UUIDv5 derivado de uma autorização (estorno, expiração, compensação); o `limites` só usa a correlação na resposta e em log, e o item do limite não grava nenhum dos três. O consumidor contábil não lê a correlação: o lançamento guarda o `id` do evento (`id_evento_origem`) e cita a autorização ou a reserva no histórico, e é pelo evento que se chega ao `correlation_id` e ao `traceparent`.
- **`LimiteAlterado` não tem essa cadeia.** O fato nasce no core legado (feed da ACL), não numa requisição, e o item do `limites` não grava correlação (`_para_item` em `limites/adapters/outbound/dynamodb.py`), então o CDC não tem o que propagar: o `correlationid` é sintético (UUIDv5 de `contrato|versao`, estável entre reentregas, `relay/src/relay/cdc_limites.py`), o `trace_id` sai nulo e o envelope não leva `traceparent`. Uma alteração de limite vinda do legado não se liga, por identificador, à mensagem do feed que a causou. A evolução é a ACL gravar no item um identificador da mensagem do legado e o CDC publicá-lo como correlação.
- O `X-Correlation-Id` recebido só é aceito se for UUID canônico; senão é gerado, o que protege contra log injection.
- **Na AWS:** X-Ray no API Gateway, access log JSON com `requestId`, `extendedRequestId` e latências (o registro por requisição: o access log do uvicorn foi desligado por custar CPU, `docs/capacidade.md`). No `autorizacao`, logs de erro com `correlation_id` e `gateway_request_id` (o `requestId` do gateway, repassado em `X-Gateway-Request-Id`) como campos; no `limites`, só com `correlation_id`. Há também linhas JSON operacionais no `limites` (guarda e reservas) e no reconciliador, que alimentam os metric filters. A instrumentação OpenTelemetry (ADOT) fica como evolução.
- **Junção borda × serviço:** a integração do gateway injeta `X-Gateway-Request-Id` = `$context.requestId`, o mesmo valor da linha do access log e do `correlation_id` das gateway responses, sobrescrevendo qualquer valor do cliente. A AWS avisa que o cliente pode influenciar o `requestId`; por isso o access log grava também o `extendedRequestId`, único e gerado pelo gateway, para desempatar.

### Trilha de auditoria

- **Todo fato de negócio vira evento imutável**, inclusive a **recusa** (`TransacaoRecusada`).
- **Produção (desenho; não está na IaC):** archive do EventBridge e Firehose → **S3 com Object Lock (compliance mode) por 10 anos** (Circular 3.978, arts. 28/67). Não há Firehose nem bucket de trilha em `infra/terraform`.
- **Sandbox: o único armazenamento durável dos eventos é o archive de 30 dias do bus** (`modules/eventos`, `retencao_archive_dias`). `TransacaoCompensada` e `TransacaoEmReconciliacaoManual` não casam com nenhuma regra (`assinaturas` em `modules/eventos/variables.tf`): ficam só no archive e somem depois de 30 dias.
- **CloudTrail do sandbox:** o trail (`desafio-zup-trail`) grava num bucket (`desafio-zup-cloudtrail-<conta>`) com Object Lock **GOVERNANCE de 1 dia** e expiração em 30 d, criados no bootstrap da conta, **fora do Terraform deste repositório**. Demonstra o mecanismo, mas não é WORM de verdade: GOVERNANCE é contornável por quem tem `s3:BypassGovernanceRetention`. A role da esteira tem `Deny` explícito dessa ação, que vence o `Allow` da `PowerUserAccess` (`infra/terraform/environments/esteira/main.tf`, `docs/operacao/infraestrutura.md`), mas uma identidade administrativa da conta continua podendo contornar (a SCP só trava região e saída da Organization). E o trail registra eventos de gerenciamento, sem eventos de dados: operação financeira não aparece nele. A trilha do negócio são os eventos.
- **Imutabilidade contábil:** correção é estorno (outro lançamento), nunca edição.

### Métricas e SLOs

- **RED por rota** (taxa, erros, duração). **Recusa (402) não conta como erro de SLO.**
- **SLI, SLO e error budget:** `docs/operacao/slo.md`. O 503 de load shedding conta como falha. A política de burn-rate em duas janelas está implementada na IaC (`modules/observabilidade`: um alarme por janela, 1 h, 5 min, 6 h e 30 min, e os compostos `api-5xx-burn-rapido` e `api-5xx-burn-lento`, que notificam); os alarmes de API são esses dois compostos e o de p99, abaixo.
- **Métricas de negócio:**
  - taxa de aprovação;
  - recusas por motivo;
  - idade da mensagem mais antiga por fila;
  - tamanho do outbox;
  - IteratorAge do relay;
  - órfãs compensadas por hora.
- **Alarmes:** 5XX por burn rate em duas janelas sobre o SLO de 99,9% (`docs/operacao/slo.md`), p99 > 400 ms (o SLO de 150 ms não foi atingido; alarmar nele dispararia sempre), qualquer mensagem em DLQ (inclusive a DLQ de entrega dos targets do EventBridge), idade da mensagem mais antiga em cada fila consumidora, `Errors` de cada Lambda, IteratorAge > 60 s do relay e do CDC, motivo de recusa desconhecido vindo do `limites`, varredura do reconciliador com erro, guarda de idempotência acima de 2.000 entradas (`OperacoesNaGuarda`) e reservas ativas no item acima de 2.000 (`ReservasAtivas`, ADR-0016), defasagem do feed do legado acima de 5 min (ADR-0014) e os do reconciliador (`reconciliador-encaminhadas`, `reconciliador-falhas`, `reconciliador-hold_perdido`). Órfãs compensadas por hora é consulta de log (runbook R4), sem alarme.
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
  - **eventos, logs e traces sem nome, CPF ou geo preciso:** os eventos levam identificadores pseudonimizados (`id_conta`, `id_contrato`), que continuam sendo dado pessoal pela LGPD (identificam a pessoa com a base do banco); o controle é minimização, não anonimização (`docs/compliance.md`);
  - `metadata` com allowlist sem geo preciso. A allowlist restringe as chaves, não o conteúdo: `id_estabelecimento` (vai no evento) e `metadata.id_rastreio_canal` (fica no item da autorização) são texto livre de até 64 caracteres, e nada no código impede um canal de pôr um CPF ali.
- **Segredos:** nenhum no código ou na imagem. O state do Terraform fica em S3 com KMS, e o `.gitignore` bloqueia `*.tfstate*`.
- **Supply chain:** dependências travadas (`uv.lock`), imagens com usuário não-root, tags imutáveis no ECR, Trivy na CI.

## Consequências

- **Positivas:**
  - Cada requisito regulatório aponta um mecanismo concreto e verificável.
  - A observabilidade separa erro técnico de recusa de negócio.
- **Negativas:**
  - **Custo de armazenamento** da trilha de 10 anos (mitigado por classes frias do S3).
  - **No sandbox, a trilha durável é de 30 dias** e não é imutável: os 10 anos em compliance mode existem só como desenho.
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
