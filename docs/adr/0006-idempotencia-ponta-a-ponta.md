# ADR-0006 — Idempotência ponta a ponta: id determinístico, lease e retomada

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-06, API-02, API-05, API-11

## Contexto

O enunciado exige `Idempotency-Key` em toda operação mutante, 200 no replay e 201 na primeira execução. Cenários reais que precisam dar o mesmo resultado:

- **Retry do canal depois de timeout.** O canal não sabe se a primeira tentativa foi aplicada.
- **Duas requisições simultâneas com a mesma chave**, por exemplo um retry agressivo do canal.
- **Processo que morre no meio.** A reserva foi aplicada no `limites`, mas a autorização não foi concluída.
- **Retry tardio.** Adquirentes repetem por horas; a reserva vive 7 dias.

Fatos: o draft IETF `Idempotency-Key` (-07) expirou em 18/04/2026 e não é RFC. Ele recomenda 400 para chave ausente, 422 para chave reutilizada com outro corpo e 409 para chave em andamento. A Stripe não guarda resultado de requisição que falhou na validação.

## Drivers

1. Nunca reservar ou debitar duas vezes, inclusive com commit incerto.
2. O replay devolve **o resultado original**, inclusive recusa (402 de novo).
3. A janela de idempotência cobre a vida inteira da reserva.
4. Nada de tabela ou cache extra no caminho quente: menos uma escrita, menos uma falha possível.

## Decisão

1. **`id_autorizacao` determinístico = UUIDv5 do escopo `(client_id, método, URI concreta, chave)`.**
   - O mesmo item do DynamoDB é o registro de idempotência **e** a autorização. Não existe tabela de idempotência separada.
   - A janela de idempotência é a retenção quente da autorização (30 dias), maior que a reserva (7 dias).
   - A URI concreta contém o `id_contrato`: a mesma chave em contratos diferentes não colide.
   - `client_id` vem do token (o gateway injeta). Clientes diferentes nunca colidem.
2. **Impressão digital** = SHA-256 do JSON canônico do **conteúdo semântico normalizado** (chaves ordenadas, sem espaços, no espírito do JCS da RFC 8785). `150` e `150.00` são o mesmo pedido.
3. **Precedência:** sintaxe (400/415, nunca registrada) → idempotência → regra de domínio (422) → 404 → recusa (402) → indisponibilidade (503).
4. **Reexecução** (`domain/idempotencia.py::avaliar_reexecucao`):

   | Situação gravada | Resposta |
   |---|---|
   | Impressão digital diferente | 422 `idempotency-key-reutilizada` |
   | Concluída (`resultado` gravado) | **replay** do resultado original (201→200 com `Idempotent-Replayed: true`; recusa → o mesmo status) |
   | `EM_PROCESSAMENTO` com lease válido (10 s) | 409 `requisicao-em-andamento` + `Retry-After` |
   | `EM_PROCESSAMENTO` com lease vencido | **retoma**: CAS no lease e continua. O `limites` é idempotente pelo `id_operacao` |
   | `COMPENSANDO` ou `COMPENSADA` (o reconciliador decidiu desfazer uma órfã) | 409 `conflito-de-estado`; o canal deve usar uma chave nova |
   | Lease vencido **e** mais de 4 min desde a criação, ou `RECONCILIACAO_MANUAL` | 409 `conflito-de-estado` **sem** `Retry-After` e **sem** reenviar ao `limites` (item 8): o desfecho sai de análise; o canal consulta com GET |

5. **`resultado` separado de `status`.** Uma aprovação depois estornada continua respondendo "aprovada" ao replay da chave original. O replay usa os campos gravados (inclusive o `correlation_id` original), e o corpo é idêntico byte a byte.
6. **Idempotência também a jusante.**
   - O `limites` deduplica pelo `id_operacao` (guarda no item, ADR-0005).
   - O estorno usa `id_operacao` = UUIDv5 derivado do id da original (`id_operacao_de_estorno`), então estornos concorrentes da mesma autorização viram **uma** operação.
   - **Um estorno em voo por original.** Como o id é derivado da original e não do estorno, o prazo de reaplicação de um estorno não protege contra outro estorno da mesma original criado minutos depois (o 1º aplicou e parou; o 2º reenviaria o mesmo id fora da guarda). Antes de chamar o limite, o estorno grava `estorno_em_curso` na original por CAS (`marcar_estorno`). Outro estorno encontra a marca e termina recusado com `ESTORNO_EM_ANDAMENTO` (409), sem tocar no limite. Marca de um estorno que terminou sem aprovação (recusado ou compensado) é assumida por CAS; dono desconhecido (item apagado pelo TTL) **não** prova que terminou e bloqueia (fail-closed). Achado na revisão do prazo de reaplicação.
   - A conclusão usa `TransactWriteItems` com `ClientRequestToken` = hash do conteúdo da transação.
7. **Reconciliador.** Trata autorizações em curso com lease vencido há mais de 60 s:
   - RESERVA e DEBITO **compensam**, numa máquina de estados **gravada passo a passo**:
     1. grava `COMPENSANDO`: daqui em diante o cliente não retoma a chave, então reconciliador e cliente nunca disputam o mesmo efeito;
     2. **pergunta** ao `limites`, numa leitura consistente que não escreve nada (`GET .../operacoes/{id}`), se a operação está aplicada: DEBITO → o id está na guarda; RESERVA → o hold está ativo. Não está → `COMPENSADA` sem nenhuma escrita. Está → grava `aplicacao_confirmada_em`;
     3. grava `desfazer_desde` e desfaz com id derivado → `COMPENSADA`.
   - Cada passo relê o relógio, renova o lease e é gravado com CAS **antes** da chamada ao limite; cada passo faz no máximo uma chamada (≤ ~4 s com retry) dentro de um lease de 10 s. A rodada seguinte continua do último passo gravado. Compensar e encaminhar publicam evento (`TransacaoCompensada`, `TransacaoEmReconciliacaoManual`): a trilha durável inclui os casos de efeito incerto.
   - **Por que a leitura decide:** o `autorizacao` manda `X-Prazo-Ms` (agora + 2 s) em cada tentativa, e o `limites` não escreve se o prazo venceu enquanto a requisição esperava na fila dele. Somado ao timeout do SDK do DynamoDB, nenhuma escrita da operação original cai mais que alguns segundos depois do envio; o reconciliador só age com o lease vencido há mais de 60 s.
   - Como numa rede de cartão, a transação cujo resultado o canal nunca recebeu é tratada como não autorizada, e nada é aplicado para "descobrir" o estado.
   - CREDITO, ESTORNO e captura **completam** (roll forward): recompor limite, desfazer e liquidar um hold já aprovado não aumentam a exposição.
   - **Correções dos painéis de juízes (20/10):**
     - painel 3: a 1ª versão fazia "garantir + desfazer" em memória, sem gravar progresso. Se o desfazer falhasse até o prazo, a órfã era encaminhada com o efeito que o próprio reconciliador criou; e um compensar mais lento que o lease deixava o cliente retomar e aprovar enquanto o reconciliador desfazia (APROVADA com débito zero);
     - painel 4: mesmo gravada, a "garantia" aplicava a operação para descobrir se ela existia. Débito órfão que nunca chegou ao limite ganhava um débito fantasma; se a reversão fosse recusada (pagamento no meio), o cliente pagava por uma compra não autorizada. E inferir "nunca aplicou" de uma recusa da garantia dependia da ordem de diagnóstico de outro BC. A consulta só de leitura elimina as duas coisas;
     - os casos têm teste: `test_reconciliador.py` (fake com guarda que esquece e idempotência por hold), `tests/integracao_bcs` (com o `limites` real).
8. **Prazo de reaplicação: 4 min contados da criação.** A guarda do `limites` dura 5 min (contrato `limites-interno-v1`). Toda aplicação de uma operação acontece depois de `criada_em`, então reenviar até `criada_em + 4 min` cai sempre dentro da guarda, com 1 min de folga para relógio e latência. Depois disso, reenviar poderia aplicar duas vezes (a guarda já pode ter esquecido o id). Então:
   - a retomada pelo cliente responde 409 `conflito-de-estado` sem `Retry-After` e **não** chama o `limites`;
   - o reconciliador **encaminha** a órfã para `RECONCILIACAO_MANUAL`, com o motivo (`FORA_DO_PRAZO`, `DESFAZER_FORA_DO_PRAZO`, `DESFAZER_RECUSADO`), o progresso e um evento. Ela sai do índice de órfãs, entra na **fila humana** (mesmo GSI, prefixo `RM#`) **sem TTL** e com alarme. Uma pessoa decide pelo PITR da tabela `limites` (runbook R7);
   - a reversão da compensação tem o mesmo prazo, contado da 1ª tentativa (`desfazer_desde`);
   - **RESERVA não depende do prazo:** o hold é uma guarda durável (só a própria compensação remove o hold de uma autorização não aprovada; contrato `limites-interno-v1`), então perguntar pelo hold e liberá-lo são seguros a qualquer tempo;
   - em operação normal o reconciliador pega a órfã em ~70–100 s; o prazo só é atingido com o reconciliador parado por minutos ou o `limites` fora por minutos.

## Consequências

- **Positivas:**
  - Zero escrita extra para idempotência.
  - Replay exato.
  - Retry seguro em todas as camadas: canal → autorização → limites → banco.
  - Coberto por testes: replay sem nova chamada ao limite, mesma chave em contrato diferente, chave reutilizada, retomada após o `limites` cair, commit incerto, dois estornos concorrentes (`tests/unit/test_autorizar_transacao.py`).
- **Negativas:**
  - O id da autorização é derivado da chave do cliente, então não é aleatório. Não vaza nada: é hash com namespace.
  - Lease sem *fencing token* depende de limites de tempo: timeout HTTP ~2 s ≪ lease 10 s ≪ graça do reconciliador 60 s ≪ prazo de reaplicação 4 min < guarda do limite 5 min. As duas últimas são acopladas por contrato entre BCs (o `limites` publica a janela; o `autorizacao` a respeita), e quem mudar uma precisa mudar a outra.
  - A janela curta da guarda (5 min) é escolha de vazão (ADR-0016), não de conforto: retry de horas (DLQ, Lambda assíncrona, EventBridge) **nunca** reenvia operação ao `limites`. Esses caminhos carregam eventos para consumidores com inbox próprio (contabilização), não comandos para o limite.
- **Riscos:** relógio desajustado entre instâncias.
  - O **lease** é seguro: é comparado pelo valor gravado (CAS).
  - A **guarda do limite não é**: a compactação compara o relógio da task que escreve com o instante gravado por outra task, e o prazo de reaplicação usa um terceiro relógio (`criada_em`). A folga entre prazo (4 min) e guarda (5 min) é de **60 s** para o desvio somado dos relógios. A primeira versão deste ADR dizia "nunca por 'agora' de duas máquinas" e "margens de uma ordem de grandeza", o que estava errado para a compactação (o painel reproduziu débito dobrado com 2 min de desvio).
  - Mitigação: o relógio das tasks Fargate é medido pelo Amazon Time Sync Service e exposto no endpoint de metadados v4 (`ClockDrift.ClockErrorBound`, `ClockSynchronizationStatus`). Um post de arquitetura da AWS cita desvio de poucos milissegundos entre tasks da mesma região; não é SLA, mas é três ordens de grandeza abaixo dos 60 s. Um teste acopla as constantes dos dois BCs (`tests/integracao_bcs`: prazo + 60 s ≤ guarda).
  - Evolução: publicar o `ClockErrorBound` como métrica (sidecar ou no próprio serviço) com alarme bem abaixo de 60 s. Se a plataforma mudar para onde o sincronismo não é medido, aumentar a guarda antes de qualquer outra coisa.

## Quando esta decisão deixa de ser boa

- **Execuções de minutos**, por exemplo chamada síncrona a um legado lento: o lease por tempo vira frágil. Aí entra fencing token (versão da autorização enviada ao `limites` e checada na escrita) ou orquestração com Step Functions.
- **O padrão IETF virar RFC com semântica diferente**: ajusta-se o mapeamento de status, e o mecanismo continua o mesmo.

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Tabela de idempotência separada com TTL de 24 h | Uma escrita a mais no caminho quente, e a janela de 24 h é menor que a reserva de 7 dias (retry no dia 2 duplicaria o hold) |
| Cache (ElastiCache) para chaves | Não é durável. Uma falha do cache faz o replay virar reexecução |
| Idempotência só no gateway | O gateway não sabe se a operação foi aplicada a jusante |
| Powertools Idempotency | Ótimo para Lambda; aqui o caminho síncrono roda em Fargate e a semântica de replay de recusa e retomada é específica do domínio |

## Referências

- Fargate, metadados v4 (`ClockDrift`): https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate-response.html
- Clock accuracy no ECS Fargate: https://aws.amazon.com/blogs/containers/run-time-sensitive-workloads-on-ecs-fargate-with-clock-accuracy-tracking/
- Desvio entre tasks Fargate (post de arquitetura, não SLA): https://aws.amazon.com/blogs/architecture/building-resilient-real-time-streaming-workers-with-amazon-dynamodb-leases/

- IETF draft-ietf-httpapi-idempotency-key-header-07: https://www.ietf.org/archive/id/draft-ietf-httpapi-idempotency-key-header-07.html
- Stripe — Idempotent requests: https://docs.stripe.com/api/idempotent_requests
- AWS Builders' Library — Making retries safe with idempotent APIs: https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
- RFC 4122/9562 (UUIDv5): https://www.rfc-editor.org/rfc/rfc9562 · RFC 8785 (JCS): https://www.rfc-editor.org/rfc/rfc8785
- Kleppmann, M. — How to do distributed locking (leases e fencing tokens): https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
