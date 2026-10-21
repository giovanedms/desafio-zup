# Runbooks

Formato: **Sintoma → Diagnóstico → Ação → Verificação**. Os alarmes citados estão em `infra/terraform/modules/observabilidade` e notificam (ao disparar e ao voltar a OK) o tópico SNS `desafio-autorizacao-alarmes` (output `topico_alarmes`). No sandbox ninguém assina por padrão: `email_alarmes` no tfvars, ou uma assinatura do plantão no tópico. Os nomes completos levam o prefixo `desafio-autorizacao-`; a descrição de cada alarme aponta o runbook.

| Alarme | Runbook |
|---|---|
| `dlq-<fila>` | R1 |
| `relay-iterator-age` | R2 |
| `reconciliador-encaminhadas` | R7 e R8 |
| `reconciliador-falhas`, `reconciliador-hold_perdido`, `reconciliador-varreduras_com_erro`, `guarda-de-idempotencia`, `reservas-ativas-no-item` | R8 |
| `defasagem-do-legado` | R9 |
| `api-5xx-burn-rapido`, `api-5xx-burn-lento` | R10 |
| `api-latencia-p99` | R11 |
| `lambda-erros-<função>`, `relay-cdc-limites-iterator-age` | R12 |
| `idade-da-fila-<fila>` | R13 |
| `motivo-desconhecido-do-limite`, `cdc-registro-descartado` | R14 |

## R1. Mensagens na DLQ de um consumidor

- **Sintoma:** alarme `dlq-<fila>` (mensagens visíveis na DLQ > 0). A DLQ só recebe o que esgotou as 5 tentativas: consumidor parado **sem** erro (fila só envelhecendo) não chega aqui, aparece no R13.
- **Diagnóstico:**
  1. ler 1 mensagem da DLQ (`aws sqs receive-message`);
  2. procurar o `id` do CloudEvent nos logs do consumidor;
  3. classificar: evento inválido (bug no produtor ou schema), dependência fora ou bug no consumidor.
- **Ação:** corrigir a causa e **redrive** da DLQ para a fila original (`aws sqs start-message-move-task`). Reprocessar é seguro porque o consumidor é idempotente (inbox).
- **Verificação:** DLQ zerada; inbox contém os ids; nenhum lançamento duplicado.
- **Caso especial, `financas-entrega-dlq` (DLQ de entrega dos targets do EventBridge):** aqui o evento **não chegou** à fila do consumidor. A regra tentou por até 1 h (ou nem tentou, em erro de permissão) e desistiu.
  1. Ler os atributos da mensagem: `ERROR_CODE` (ex.: `NO_PERMISSIONS` = queue policy da fila do consumidor quebrada; `THROTTLING`), `ERROR_MESSAGE`, `RULE_ARN` e `TARGET_ARN` (a fila de destino).
  2. Corrigir a causa (normalmente a queue policy do consumidor ou a fila apagada).
  3. Reenviar cada mensagem para a fila do seu `TARGET_ARN` (o corpo já é o evento no formato que o consumidor recebe) e apagar da DLQ. O `start-message-move-task` não serve aqui: a DLQ é de todos os targets, e o destino muda por mensagem. Repetir é seguro (consumidores idempotentes).
  ```bash
  # Lê 10 mensagens com os atributos do EventBridge (só o TARGET_ARN e o ERROR_CODE na tela)
  aws sqs receive-message --profile desafio-zup --queue-url <url da financas-entrega-dlq> \
    --max-number-of-messages 10 --message-attribute-names All \
    --query 'Messages[].[MessageId, MessageAttributes.TARGET_ARN.StringValue, MessageAttributes.ERROR_CODE.StringValue]'
  ```

## R2. Outbox crescendo / relay atrasado

- **Sintoma:** alarme `IteratorAge` do relay > 60 s, ou itens antigos na tabela `autorizacoes-outbox`.
- **Diagnóstico:** erros da Lambda relay (PutEvents throttled? quota?); estado do event source mapping; linha `{"msg": "sweeper", ...}` no log do `desafio-sweeper` (`publicados`, `falhos`, `varridos`, `interrompida`).
- **Ação:**
  - throttling → pedir aumento de quota de PutEvents;
  - erro de código → corrigir e reimplantar.
  - O **sweeper** republica o que passou de 15 min, mesmo com o Stream expirado (24 h). A drenagem é **incremental**: por rodada (a cada 5 min) ele lê o outbox em páginas de 500 itens (`Limit` do Scan), publica cada página antes de ler a próxima e para em **10.000 itens** ou em **40 s** (abaixo do timeout de 60 s da Lambda), o que vier primeiro. Ou seja, **no máximo N = 10.000 por rodada**: `interrompida: true` no log quer dizer que sobrou backlog para a rodada seguinte, não falha.
  - Backlog muito maior que isso (ex.: relay parado por horas a milhares de TPS): o caminho de recuperação é o **relay** voltar a ler o Stream (24 h de retenção); o sweeper não drena milhões de itens. Se o Stream já expirou, invocar o sweeper à mão em sequência (`aws lambda invoke --function-name desafio-sweeper`) até `interrompida: false`; cada invocação é idempotente (consumidores deduplicam por `(source, id)`).
- **Verificação:** outbox volta a quase zero; IteratorAge < 1 s; o sweeper loga `interrompida: false`.

## R3. Aumento de quota antes de pico (início de mês, campanha)

- **Quotas** (ajustáveis, com prazo de dias; conta em `docs/capacidade.md`, seção 1):
  - EventBridge **PutEvents 12.000/s** e **Invocations 25.000/s** (as aprovadas, 6.000 e 20.000, não cobrem o mix com captura);
  - vCPU de Fargate ~200 (a conta tem 140) e `tasks_max` compatível;
  - concorrência de Lambda e throttle do API Gateway, se o pico passar do desenho. O ESM da contabilização tem teto de 200 em voo (`concorrencia_maxima_sqs`), dimensionado para ~10.000 eventos/s com lotes de ~100 ms (conta em `modules/lambdas/variables.tf`); acima disso, subir o teto no tfvars (até 1.000, que é a conta inteira) e acompanhar o alarme `idade-da-fila-contabilizacao` (R13).
- **DynamoDB:** `warm_throughput` no tfvars (módulo `dados`) nas tabelas `limites`, `autorizacoes` (e no `gsi_orfas`) e `autorizacoes-outbox` a 2× o pico esperado, com pelo menos 1 dia de antecedência. Valores no ADR-0013. **Não dá para desfazer** (a AWS não deixa reduzir) e o aumento é cobrado: só em produção ou com decisão explícita.
- **ECS:** janela em `janelas_de_pico` (tfvars). O default do sandbox já sobe o mínimo para 4 tasks no dia 1 de cada mês, 06:30–12:00 de Brasília. Para uma campanha avulsa, acrescentar uma janela com `inicio`/`fim` em `cron(...)` ou `at(...)`.
  - **Não fazer apply durante a janela:** o apply devolve o mínimo do alvo de autoscaling ao valor de base (`tasks_min`), e a capacidade pré-aquecida pode descer. Se for inevitável, subir `tasks_min` no mesmo apply.
- **Shards dos GSIs esparsos** (`shards_dos_indices`, padrão 64): para um pico acima de ~5.000 TPS sustentados, subir antes (o leitor e o escritor sobem juntos no mesmo apply). Nunca reduzir.

## R4. Órfãs compensadas acima do basal

- **Sintoma:** a contagem `orfa_COMPENSADA` na linha de rodada do reconciliador sobe (consulta no CloudWatch Logs; os alarmes por métrica do reconciliador cobrem encaminhadas, falhas e hold perdido, ver R8).
- **Diagnóstico:** processos morrendo (OOM? deploy no meio de requisições?) ou `limites` lento (timeouts no `autorizacao`). O gateway corta em 10 s (`timeout_integracao_ms`): uma autorização que passou disso devolveu 504 ao canal e pode ter terminado depois (o replay com a mesma chave resolve; R11 tem o diagnóstico de latência).
- **Ação:** corrigir a causa. Compensação que chega a `COMPENSADA` tem efeito líquido zero; a que não consegue terminar com segurança vai para a fila humana (R7) com o progresso gravado. O canal recebe 409 numa retomada tardia e deve gerar nova chave.

## R5. Failover regional (DR — desenhado)

1. Declarar o incidente e congelar escrita na primária (flag / SCP).
2. Promover a secundária (global table já replicada).
3. Apontar o DNS do API Gateway regional da secundária.
4. Reconciliar o RPO: autorizações dos últimos segundos antes da falha, comparadas com o legado e com os eventos arquivados.
5. Comunicar o BCB se o dado passar a ser processado fora do país (Res. CMN 4.893, arts. 15–17).

## R6. Deploy e destroy da janela de medição

- **Caminho padrão: a esteira.** Push na `main` → CI → deploy (tag `git-<sha12>`, imagens linux/amd64 construídas, escaneadas com `trivy image` e só então publicadas; tasks X86_64) → smoke. **Deploy reprovado por CRITICAL na imagem:** ler o CVE no log do job; com correção, atualizar a base (PR do Dependabot ou novo digest no Dockerfile) ou a dependência; sem correção e com risco aceito, `.trivyignore` na raiz com CVE, motivo e data, revisado em PR. Destroy: `gh workflow run esteira.yml -f acao=destroy -f confirmacao="CONFIRMO: destroy na conta desafio-zup"` (só na `main`). Detalhes em `docs/operacao/infraestrutura.md`, seção 6.
- **Caminho local** (mesmo state; o lock do S3 impede dois applies ao mesmo tempo, mas o próximo push na `main` volta as imagens para a tag do commit):
  - `make deploy EXPECTED_ACCOUNT_ID=<conta> CONFIRMO="CONFIRMO: deploy na conta desafio-zup" [TAG=1.0.4] [ARQ=amd64|arm64]`
  - O alvo faz, em ordem: pacotes das Lambdas → `apply -target=module.ecr` → `make imagens` (build + push; tag que já existe é pulada, porque as tags do ECR são imutáveis) → apply completo com a tag e a arquitetura das tasks (`ARQ=amd64` → X86_64; `arm64` → ARM64, que exige buildx com emulação ou máquina ARM).
  - Versão nova de código = `TAG` nova (ex.: 1.0.5). O `make imagens` não roda o `trivy image`: rodar à mão antes (`trivy image --severity CRITICAL --exit-code 1 <imagem>`).
- **Janela de medição (k6):** o `plano-carga` nasce estacionado em 200 rps. Antes dos degraus, subir no tfvars os três juntos (`plano_carga`, `waf_limite_por_consumidor`, `waf_limite_por_ip`, valores em `terraform.tfvars.example`) e aplicar; ao terminar, voltar ao default e aplicar de novo. Uma API key com 6.000 rps esquecida no ar é 60% do throttle da conta.
- **Ambiente `esteira`** (role, boundary e o perímetro da trilha): **nunca** pela própria esteira. O administrador aplica com o profile administrativo (`docs/operacao/infraestrutura.md`, seção 3).
  - **Encerrar:** `make destroy ... CONFIRMO="CONFIRMO: destroy na conta desafio-zup"`.

## R7. Reconciliação manual (fila humana `RM#`)

- **Sintoma:** alarme `reconciliador-encaminhadas`, ou um GET da autorização mostra `RECONCILIACAO_MANUAL`. Se `reconciliador-varreduras_com_erro` disparou junto, tratar antes o R8 (a varredura que levanta não encaminha nada: o encaminhamento é desfecho de negócio). O canal recebe 409 `conflito-de-estado` **sem** `Retry-After` (repetir não muda o desfecho).
- **Listar a fila:** as autorizações (e reservas sem hold) encaminhadas estão no GSI `gsi_orfas` com `orfa_shard` = `RM#0`..`RM#<N-1>`, ordenadas pela data do encaminhamento. N é `shards_dos_indices` (padrão 64; itens gravados antes de 20/10 usaram 10, e caem dentro de 0..63):
  ```bash
  # Fila humana, shard por shard (o reconciliador só lê EP#; RM# é só desta fila). N = shards_dos_indices.
  N=64
  for n in $(seq 0 $((N - 1))); do
    aws dynamodb query --profile desafio-zup --table-name autorizacoes --index-name gsi_orfas \
      --key-condition-expression "orfa_shard = :s" --expression-attribute-values "{\":s\":{\"S\":\"RM#$n\"}}" \
      --projection-expression "id_autorizacao, #st, tipo, id_contrato, motivo_da_reconciliacao, criada_em, aplicacao_confirmada_em, desfazer_desde" \
      --expression-attribute-names '{"#st":"status"}'
  done
  ```
- **Diagnóstico pelo motivo gravado** (`motivo_da_reconciliacao`). A pergunta é sempre "o id X foi aplicado no limite?", e a guarda de 5 min já esqueceu. Resposta determinística: a tabela `limites` tem PITR; restaurar para uma **tabela temporária** no instante indicado e ver se o mapa `ops` do contrato contém o id (a guarda de 5 min garante que ele ainda estaria lá nesse instante).

  | Motivo | O que se sabe | O que perguntar ao PITR |
  |---|---|---|
  | `FORA_DO_PRAZO` | DEBITO, CREDITO, ESTORNO ou captura: a operação original pode ou não ter aplicado, e a guarda já pode ter esquecido o id | `ops` contém o `id_autorizacao` em `criada_em + 4 min`? (estorno: o id derivado da original) |
  | `DESFAZER_FORA_DO_PRAZO` | o limite **confirmou** que a operação original estava aplicada (`aplicacao_confirmada_em`) | `ops` contém o id da compensação (UUIDv5 de `id_autorizacao`, `id_operacao_de_compensacao`) em `desfazer_desde + 4 min`? |
  | `DESFAZER_RECUSADO` | aplicada (confirmado); o limite recusou a reversão (ex.: pagamento entrou no meio) | nada: é decisão de negócio (manter o débito ou creditar) |
  | `RESERVA_SEM_HOLD` (status `RECONCILIACAO_MANUAL`, `resultado` APROVADA) | o hold sumiu e ninguém encerrou a reserva em 10 min | quem removeu: procurar na fila uma captura/estorno desta reserva; ou `ops` contém o id derivado da expiração |

  Dentro de 24 h, o Stream da tabela `limites` também mostra o MODIFY que acrescentou a chave em `ops`.
- **Ação:** concluir com a decisão (APROVADA com os eventos, RECUSADA ou COMPENSADA, desfazendo no limite o que precisar). Apagar a tabela temporária no fim.
  - **Tirar da fila:** o item na fila não tem TTL e carrega `orfa_shard`/`orfa_lease_ms` com prefixo `RM#`. A conclusão regrava o item inteiro (como o `concluir` faz) ou, se for por Update, precisa de `REMOVE orfa_shard, orfa_lease_ms` e de `SET ttl` (retenção quente de 30 dias a partir de agora). Sem isso o item continua na fila.
  - A trilha durável do caso é o evento `TransacaoEmReconciliacaoManual` (motivo e progresso); a decisão manual também deve publicar o evento do desfecho.
  - Estorno encaminhado segura a marca `estorno_em_curso` da original. Concluído como RECUSADA/COMPENSADA, a marca vira "velha" e o próximo estorno a assume sozinho; concluído como APROVADA, a original vai para ESTORNADA. Não é preciso limpar a marca à mão.
- **Lacuna conhecida:** não existe ferramenta administrativa no código para essa conclusão. Ela leria a autorização, aplicaria a decisão com a mesma transação do `concluir` (status final, evento no outbox, original encerrada quando houver) e registraria quem decidiu. Até existir, a conclusão é manual e revisada por uma segunda pessoa.
- **Verificação:** a autorização saiu de `RECONCILIACAO_MANUAL` (e da fila `RM#`), o saldo do contrato bate com a decisão, o evento correspondente está no outbox e a tabela temporária foi apagada.

## R8. Alertas do ciclo de vida do hold e do contrato quente

- **Alarme `reconciliador-encaminhadas`** (métrica `OrfasEncaminhadas`, ≥ 1 rodada em 5 min com `orfa_ENCAMINHADA` ou `reserva_ENCAMINHADA`): entrou item na fila humana. Seguir R7.
- **Alarme `reconciliador-hold_perdido`** (métrica `ReservasSemHold`, ≥ 20 rodadas em 15 min com `reserva_PERDEU_O_HOLD`): reservas vencidas sem hold e sem vencedor. Uma contagem que aparece e some é a corrida normal com captura ou estorno; a mesma reserva por 1 h vira `reserva_ENCAMINHADA` sozinha (R7, motivo `RESERVA_SEM_HOLD`). O alarme serve para ver o volume antes disso.
- **Alarme `reconciliador-falhas`** (métrica `FalhasDoReconciliador`, ≥ 6 rodadas em 5 min com `orfa_FALHOU` ou `reserva_FALHOU`): `limites` fora, contrato que sumiu ou `RESERVA_DIVERGENTE` (bug). Investigar nessa ordem; se for `RESERVA_DIVERGENTE`, abrir bug. Compensação que falha fica em `COMPENSANDO` com o progresso gravado e continua sozinha quando o `limites` voltar (débito com reversão parada há mais de 4 min vai para a fila, motivo `DESFAZER_FORA_DO_PRAZO`).
- **Alarme `OperacoesNaGuarda`** (namespace `desafio-zup/limites`, `Maximum` > `limite_guarda_operacoes`, hoje 2000): há um contrato quente. Abrir a regra de Contributor Insights `guarda-por-contrato` (top contratos) e as tabelas `limites` e `autorizacoes` no Contributor Insights do DynamoDB, e avaliar a fragmentação (ADR-0016).
- **Alarme `reservas-ativas-no-item`** (métrica `ReservasAtivas`, `Maximum` > `limite_reservas_ativas`, hoje 2000): um contrato acumula holds pendentes (até 7 dias cada) dentro do item. Com ~9.000 o item chega a 400 KB e **toda** escrita do contrato falha. Diagnóstico: o `id_contrato` está na linha JSON `guarda_de_idempotencia` em `/ecs/limites`. Ação imediata: com o negócio, capturar ou estornar os holds velhos do contrato (cada um sai do item); estrutural: fragmentar o contrato (ADR-0016). Verificação: `ReservasAtivas` do contrato caindo.
- **Alarme `reconciliador-varreduras_com_erro`** (métrica `RodadasComVarreduraComErro`, ≥ 2 rodadas em 2 min cuja linha traz o campo `varreduras_com_erro`): uma varredura (órfãs ou reservas) **levantou exceção**, o que é diferente de `FALHOU` (desfecho tratado). O heartbeat só renova em rodada sem exceção, e o healthcheck reprova com ele acima de 90 s: com 2 rodadas com erro, a task está a uma rodada de ser reciclada pelo ECS. Diagnóstico: o traceback está em `/ecs/reconciliador` logo antes da linha da rodada (`varredura de órfãs falhou` / `varredura de reservas vencidas falhou`); o campo diz qual das duas. Causas prováveis, nesta ordem: DynamoDB estrangulando ou fora (`ThrottlingException`, timeouts), permissão (`AccessDeniedException` depois de mudança na task role), bug. A reciclagem não resolve causa externa: se o alarme continua depois da task nova, corrigir a causa. A varredura que não levantou continua trabalhando na mesma rodada.
- **Como os alarmes do reconciliador contam:** a linha de rodada não é JSON puro (tem prefixo de data e logger), então o metric filter casa o termo (`"orfa_ENCAMINHADA"`, etc.) e cada rodada com o desfecho vale 1. Para saber QUAL autorização ou reserva, listar a fila `RM#` (R7).
- **Verificação:** a contagem volta a zero nas rodadas seguintes e as reservas envolvidas estão em estado final (CAPTURADA, ESTORNADA ou EXPIRADA) ou na fila `RM#`.

## R9. Defasagem do feed do legado (alarme `defasagem-do-legado`)

- **Sintoma:** a mensagem mais antiga da fila `legado-contratos` espera há mais de 5 min. O limite concedido da réplica está velho: uma redução feita no core ainda não vale aqui (risco aceito no ADR-0014, limitado por este alarme).
- **Diagnóstico:**
  1. o service `limites-acl` está rodando (`runningCount` 1) e o heartbeat está saudável? Logs em `/ecs/limites-acl`;
  2. o volume do feed subiu (carga em lote do legado)? `NumberOfMessagesSent` da fila;
  3. mensagens repetindo até a DLQ (`legado-contratos-dlq` com alarme próprio): registro fora do layout.
- **Ação:** task parada → forçar novo deployment do `limites-acl`; volume → mais tasks no `limites-acl` (o compare-and-set por versão converge em qualquer ordem, mas mais de um consumidor é inferência sem teste: `docs/operacao/infraestrutura.md`, seção 4.3); registro inválido → R1. Se a defasagem for longa e houver redução de limite relevante pendente (fraude), escalar para o negócio: o canal prioritário de bloqueio é gatilho de evolução do ADR-0014, não existe hoje.
- **Verificação:** `ApproximateAgeOfOldestMessage` abaixo de 300 s e o alarme de volta a OK. As outras filas consumidoras têm o alarme genérico de idade (R13); esta tem o próprio porque o limiar vem do risco aceito no ADR-0014.

## R10. SLO de disponibilidade em risco (alarmes `api-5xx-burn-rapido` e `api-5xx-burn-lento`)

- **Sintoma:** um dos alarmes compostos de burn rate. O SLO é 99,9% ao mês (ADR-0001), ou seja, 0,1% de orçamento de erro. O **rápido** dispara com a taxa de 5XX acima de 1,44% (14,4× o orçamento) na janela de 1 h **e** na de 5 min: nesse ritmo, 2% do orçamento do mês vão embora em 1 h, e o mês inteiro em ~2 dias. O **lento** dispara com mais de 0,6% (6×) em 6 h **e** em 30 min: 5% do orçamento em 6 h. A janela curta faz o alarme voltar a OK logo que o problema acaba. Janela com média abaixo de 1 rps não avalia (sem amostra para medir 0,1%).
- **O que conta como erro:** todo `5XXError` do stage `v1`, **inclusive o 503 do load shedding** e o 504 de timeout da integração. É de propósito: o shedding protege a frota, mas o canal recebeu erro. Um burn rate alto com 503 não é "o mecanismo funcionando, ignorar": é capacidade abaixo da demanda, e consome o orçamento.
- **Diagnóstico:**
  1. qual 5XX? No access log da borda (`/aws/apigateway/desafio-autorizacao-v1`), contar por `status` na janela do alarme;
  2. **503:** shedding. CPU do `autorizacao` no dashboard, tasks em execução contra `tasks_max`, se o target tracking já pediu mais tasks (leva ~6 min), quota de vCPU de Fargate;
  3. **504:** a integração passou de 10 s. Ver R11 (latência) e se o `limites` ou o DynamoDB estão lentos (`IntegrationLatency`, `SuccessfulRequestLatency`);
  4. **500:** bug ou dependência quebrada. Log do serviço pelo `X-Gateway-Request-Id` da requisição (= `requestId` do access log);
  5. **502:** o NLB ou a task devolveu conexão fechada ou inválida. Alvos saudáveis no target group, deploy em andamento (circuit breaker).
- **Ação:** 503 → subir o mínimo de tasks (`tasks_min` ou uma janela em `janelas_de_pico`, R3) e, se for pico previsto, pedir quota antes da próxima vez; 504 → R11; 500 → reverter o deploy (o circuit breaker só reverte task que não fica saudável) ou corrigir; 502 → conferir o health check do NLB e o keep-alive (o `--timeout-keep-alive 400` do uvicorn precisa ficar acima dos 350 s do NLB).
- **Verificação:** os dois compostos em OK e a taxa de 5XX abaixo de 0,1% no dashboard. Registrar quanto do orçamento do mês o incidente consumiu.

## R11. Latência p99 acima do limiar (alarme `api-latencia-p99`)

- **Sintoma:** `Latency` p99 do stage `v1` acima de 400 ms em 3 de 5 minutos. O limiar **não é** o SLO: o SLO é 150 ms (linha do dashboard), e o entregue não o atinge (164 ms no melhor degrau, `docs/capacidade.md` §3). O alarme separa essa lacuna conhecida de "o serviço piorou".
- **Diagnóstico:**
  1. `Latency` × `IntegrationLatency` no dashboard: diferença grande = a borda (WAF, authorizer do Cognito); as duas juntas = o serviço;
  2. CPU do `autorizacao` e do `limites`: perto de 60% o p99 medido foi ~530 ms, então o alarme acompanha a frota sem folga;
  3. `SuccessfulRequestLatency` do DynamoDB (`limites` e `autorizacoes`) e throttling;
  4. contrato quente: alarme `guarda-de-idempotencia` e Contributor Insights (R8).
- **Ação:** frota sem folga → subir o mínimo de tasks (R3); DynamoDB lento ou estrangulado → contrato quente (ADR-0016) ou quota; borda → olhar regras do WAF e o Cognito. Fechar o SLO de 150 ms é trabalho de desenho (alavanca 1 de `docs/capacidade.md`), não deste runbook.
- **Verificação:** p99 abaixo de 400 ms por 5 min e o alarme em OK.

## R12. Lambda com erro, ou CDC de limites atrasado (alarmes `lambda-erros-<função>` e `relay-cdc-limites-iterator-age`)

- **Sintoma:** `Errors` ≥ 1 em 5 min numa das 4 funções (`relay`, `sweeper`, `cdc-limites`, `contabilizacao`), ou o `IteratorAge` do `desafio-relay-cdc-limites` acima de 60 s. `Errors` conta invocação que terminou em exceção, timeout ou falta de memória; a falha **parcial** de lote (`ReportBatchItemFailures`) não conta e aparece no `IteratorAge` e na DLQ.
- **Diagnóstico** (log group `/aws/lambda/<função>`; o `REPORT` da invocação mostra `Duration`, `Max Memory Used` e `Status: timeout` quando é o caso):
  - **`sweeper`:** o caso grave é o backlog grande no outbox (relay parado por muito tempo): a varredura não termina em 60 s ou não cabe em 256 MB. `Max Memory Used` perto do limite ou `Status: timeout` confirmam. Enquanto o sweeper falha, o outbox não é drenado por ele;
  - **`relay`:** `PutEvents` estrangulado (quota), permissão no bus, evento malformado. Ver também o R2;
  - **`cdc-limites`:** registro do Stream que o código não consegue converter ou `PutEvents` falhando. O `IteratorAge` do CDC sobe junto quando o erro é persistente: o lote é retentado até 5 vezes e depois vai para a `relay-cdc-limites-dlq` (alarme `dlq-relay-cdc-limites-dlq`, R1);
  - **`contabilizacao`:** exceção no lote inteiro (configuração, permissão, tabela, ou uma mensagem que o código não trata e derruba a invocação). A falha de uma mensagem tratada volta pela resposta parcial de lote e, depois de 5 recebimentos, vai para a DLQ (R1).
- **Ação:** causa externa (quota, permissão, DynamoDB) → corrigir e deixar o retry automático trabalhar; código → corrigir e reimplantar (push na `main`). Sweeper com backlog grande: subir à mão, temporariamente, a memória e o timeout da função para drenar (`aws lambda update-function-configuration --function-name desafio-sweeper --memory-size 1024 --timeout 300`; o próximo apply devolve os valores do Terraform) e tratar a causa do relay parado (R2). CDC atrasado sem erro: volume de escrita no `limites` acima do que os shards do Stream entregam; acompanhar até drenar.
- **Verificação:** `Errors` volta a 0, `IteratorAge` abaixo de 60 s, DLQs vazias (ou redrive feito, R1).

## R13. Fila consumidora envelhecendo (alarme `idade-da-fila-<fila>`)

- **Sintoma:** a mensagem mais antiga de `contabilizacao`, `antifraude` ou `notificacoes` espera há mais de 300 s (acima da visibilidade de 180 s, então uma falha transitória isolada não dispara). A DLQ não acusa isso: o redrive é por número de recebimentos, não por espera.
- **No sandbox:** `antifraude` e `notificacoes` **não têm consumidor** (representam sistemas a jusante, `docs/operacao/infraestrutura.md` §4.5). O alarme delas entra em ALARM 5 min depois do primeiro evento e fica assim até as mensagens expirarem (14 dias). É o sinal correto (ninguém consome); para limpar depois de uma demo, `aws sqs purge-queue` na fila. Em produção, cada alarme é do dono do consumidor.
- **Diagnóstico (`contabilizacao`):**
  1. a função está sendo invocada? `Invocations` e `Errors` de `desafio-contabilizacao` (R12 se houver erro);
  2. está no teto? `ConcurrentExecutions` da função perto de 200 (o `maximum_concurrency` do ESM) = a chegada passou do dimensionamento (~10.000 eventos/s com lotes de ~100 ms);
  3. está lenta? `Duration` subiu (DynamoDB das tabelas da contabilização estrangulando, `WriteThrottleEvents`);
  4. o ESM está habilitado? (`aws lambda list-event-source-mappings --function-name desafio-contabilizacao`).
- **Ação:** no teto → subir `concorrencia_maxima_sqs` no tfvars (R3) e aplicar; lenta por DynamoDB → ver throttling e quota da tabela; ESM desabilitado → habilitar. O consumidor é idempotente (inbox): a fila drena sem lançamento duplicado.
- **Verificação:** `ApproximateAgeOfOldestMessage` caindo até ficar abaixo de 300 s e o alarme em OK.

## R14. Sinal de contrato ou de dado descartado (alarmes `motivo-desconhecido-do-limite` e `cdc-registro-descartado`)

- **`motivo-desconhecido-do-limite`:** o `limites` devolveu um motivo de recusa que o `autorizacao` (API ou reconciliador) não conhece. O leitor tolerante recusa com `RECUSA_DO_LIMITE` (nunca 500), então o canal não quebra, mas o contrato interno mudou sem o cliente acompanhar.
  - **Diagnóstico:** a linha `{"evento": "motivo_desconhecido_do_limite", "motivo": ...}` em `/ecs/autorizacao` (ou com prefixo em `/ecs/reconciliador`) traz o valor cru. Conferir o enum de `motivo` em `contracts/openapi/limites-interno-v1.yaml` e o que está publicado no `limites` em produção (versão da imagem).
  - **Ação:** mapear o motivo novo no `autorizacao` (código de recusa e texto) e publicar; se o `limites` publicou o motivo antes da hora, reverter o `limites`. Valor novo de enum no contrato interno pede o cliente atualizado antes do produtor.
  - **Verificação:** nenhuma linha nova em 5 min e o alarme em OK.
- **`cdc-registro-descartado`:** o `desafio-relay-cdc-limites` descartou um registro do Stream da tabela `limites` sem publicar `LimiteAlterado`. O consumidor de `LimiteAlterado` (notificações) perdeu um fato.
  - **Diagnóstico:** a linha no log group do CDC traz o motivo do descarte; achar o `id_contrato` e a versão do item. Dentro de 24 h o Stream ainda tem o registro.
  - **Ação:** corrigir a causa (registro com formato que o código não espera); para o fato perdido, republicar o `LimiteAlterado` a partir do item atual do contrato, ou registrar o caso para o dono das notificações. Repetir é seguro: o `id` do CloudEvent é determinístico por contrato e versão (deduplicação no consumidor).
  - **Verificação:** nenhum descarte novo em 5 min e o alarme em OK.
- **Filtro sem linha:** os dois filtros existem mesmo que a linha nunca apareça (a métrica fica em 0). O alarme em OK não prova que o sinal está ligado; quem prova é o teste do serviço que emite a linha.
