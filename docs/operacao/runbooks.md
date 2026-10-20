# Runbooks

Formato: **Sintoma → Diagnóstico → Ação → Verificação**. Os alarmes citados estão em `infra/terraform/modules/observabilidade`.

## R1. Mensagens na DLQ de um consumidor

- **Sintoma:** alarme "mensagens visíveis em DLQ > 0".
- **Diagnóstico:**
  1. ler 1 mensagem da DLQ (`aws sqs receive-message`);
  2. procurar o `id` do CloudEvent nos logs do consumidor;
  3. classificar: evento inválido (bug no produtor ou schema), dependência fora ou bug no consumidor.
- **Ação:** corrigir a causa e **redrive** da DLQ para a fila original (`aws sqs start-message-move-task`). Reprocessar é seguro porque o consumidor é idempotente (inbox).
- **Verificação:** DLQ zerada; inbox contém os ids; nenhum lançamento duplicado.

## R2. Outbox crescendo / relay atrasado

- **Sintoma:** alarme `IteratorAge` do relay > 60 s, ou itens antigos na tabela `autorizacoes-outbox`.
- **Diagnóstico:** erros da Lambda relay (PutEvents throttled? quota?); estado do event source mapping.
- **Ação:**
  - throttling → pedir aumento de quota de PutEvents;
  - erro de código → corrigir e reimplantar.
  - O **sweeper** republica o que passou de 15 min, mesmo com o Stream expirado (24 h).
- **Verificação:** outbox volta a quase zero; IteratorAge < 1 s.

## R3. Aumento de quota antes de pico (início de mês, campanha)

- **Quotas:** PutEvents, Invocations, concorrência de Lambda, vCPU de Fargate, throttle do API Gateway (todas ajustáveis, com prazo de dias).
- **DynamoDB:** **warm throughput** nas tabelas `limites` e `autorizacoes` a 2× o pico esperado, com pelo menos 1 dia de antecedência.
- **ECS:** scheduled scaling para o mínimo de tasks antes da janela.

## R4. Órfãs compensadas acima do basal

- **Sintoma:** a contagem `orfa_COMPENSADA` na linha de rodada do reconciliador sobe (consulta no CloudWatch Logs; os alarmes por métrica do reconciliador cobrem encaminhadas, falhas e hold perdido, ver R8).
- **Diagnóstico:** processos morrendo (OOM? deploy no meio de requisições?) ou `limites` lento (timeouts no `autorizacao`).
- **Ação:** corrigir a causa. Compensação que chega a `COMPENSADA` tem efeito líquido zero; a que não consegue terminar com segurança vai para a fila humana (R7) com o progresso gravado. O canal recebe 409 numa retomada tardia e deve gerar nova chave.

## R5. Failover regional (DR — desenhado)

1. Declarar o incidente e congelar escrita na primária (flag / SCP).
2. Promover a secundária (global table já replicada).
3. Apontar o DNS do API Gateway regional da secundária.
4. Reconciliar o RPO: autorizações dos últimos segundos antes da falha, comparadas com o legado e com os eventos arquivados.
5. Comunicar o BCB se o dado passar a ser processado fora do país (Res. CMN 4.893, arts. 15–17).

## R6. Deploy e destroy da janela de medição

- **Caminho padrão: a esteira.** Push na `main` → CI → deploy (tag `git-<sha12>`, imagens linux/amd64, tasks X86_64) → smoke. Destroy: `gh workflow run esteira.yml -f acao=destroy -f confirmacao="CONFIRMO: destroy na conta desafio-zup"` (só na `main`). Detalhes em `docs/operacao/infraestrutura.md`, seção 6.
- **Caminho local** (mesmo state; o lock do S3 impede dois applies ao mesmo tempo, mas o próximo push na `main` volta as imagens para a tag do commit):
  - `make deploy EXPECTED_ACCOUNT_ID=<conta> CONFIRMO="CONFIRMO: deploy na conta desafio-zup" [TAG=1.0.4] [ARQ=amd64|arm64]`
  - O alvo faz, em ordem: pacotes das Lambdas → `apply -target=module.ecr` → `make imagens` (build + push; tag que já existe é pulada, porque as tags do ECR são imutáveis) → apply completo com a tag e a arquitetura das tasks (`ARQ=amd64` → X86_64; `arm64` → ARM64, que exige buildx com emulação ou máquina ARM).
  - Versão nova de código = `TAG` nova (ex.: 1.0.5).
  - **Encerrar:** `make destroy ... CONFIRMO="CONFIRMO: destroy na conta desafio-zup"`.

## R7. Reconciliação manual (fila humana `RM#`)

- **Sintoma:** alarme `reconciliador-encaminhadas`, ou um GET da autorização mostra `RECONCILIACAO_MANUAL`. O canal recebe 409 `conflito-de-estado` **sem** `Retry-After` (repetir não muda o desfecho).
- **Listar a fila:** as autorizações (e reservas sem hold) encaminhadas estão no GSI `gsi_orfas` com `orfa_shard` = `RM#0`..`RM#9`, ordenadas pela data do encaminhamento:
  ```bash
  # Fila humana, shard por shard (o reconciliador só lê EP#; RM# é só desta fila)
  for n in 0 1 2 3 4 5 6 7 8 9; do
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
- **Como os alarmes do reconciliador contam:** a linha de rodada não é JSON puro (tem prefixo de data e logger), então o metric filter casa o termo (`"orfa_ENCAMINHADA"`, etc.) e cada rodada com o desfecho vale 1. Para saber QUAL autorização ou reserva, listar a fila `RM#` (R7).
- **Verificação:** a contagem volta a zero nas rodadas seguintes e as reservas envolvidas estão em estado final (CAPTURADA, ESTORNADA ou EXPIRADA) ou na fila `RM#`.
