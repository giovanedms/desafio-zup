# Como ler o serviço `autorizacao` (BC Autorização de Transações)

**Uma frase:** recebe o pedido do canal, garante que **o mesmo pedido nunca é processado duas vezes**, reserva ou consome o limite no BC Limites e registra a decisão junto com o evento, numa escrita atômica.

## O caminho de um POST (siga nesta ordem)

1. **`adapters/inbound/http.py`**, na ordem de precedência do catálogo de erros:
   - 401 sem `X-Client-Id` (na AWS, o gateway injeta a partir do token);
   - 415 se o `Content-Type` estiver errado;
   - 400 se a `Idempotency-Key` for inválida;
   - 400 para JSON malformado (parse exato com `Decimal`);
   - 422 para violação do contrato ou do domínio.
2. **Monta `PedidoDeAutorizacao`** (`domain/pedido.py`): valida BRL, 2 casas e ESTORNO com a original. Monta também o **escopo** (cliente, método, URI concreta, chave).
3. **`application/autorizar.py` → `AutorizarTransacao.executar`:**
   1. `id = UUIDv5(escopo)` (`domain/idempotencia.py`). **O mesmo item é o registro de idempotência e a autorização.**
   2. Já existe? → `avaliar_reexecucao`: replay, 422 (chave reutilizada), 409 (em andamento, lease válido) ou **retomar** (lease vencido).
   3. Estorno ou captura? → valida a original (existe, mesmo contrato, APROVADA). Estorno exige o mesmo valor. **Captura** (DEBITO com `id_autorizacao_original`) exige que a original seja uma RESERVA e que o valor caiba nela (parcial permitida); original já encerrada → 409, valor que não cabe → 422.
   4. `registrar` (PutItem condicional, lease de 10 s).
   5. `processar` → `GatewayDeLimites.aplicar`, com **id da operação = id da autorização**. No estorno, o id é UUIDv5 derivado da original, então estornos concorrentes viram uma operação só. Na captura, a operação no limite é `CAPTURAR_RESERVA` (`TipoNoLimite`), com `id_reserva` e `valor_reservado` da original; quem perde o hold volta `RESERVA_NAO_ENCONTRADA` e a autorização fica RECUSADA (409 `conflito-de-estado`), sem mexer na reserva.
   6. `aprovar` / `recusar` no agregado (registra o **evento de domínio**) → `concluir`.
4. **`adapters/outbound/dynamodb.py` → `concluir`:** **uma** `TransactWriteItems` com:
   - a autorização final, condicionada a "EM_PROCESSAMENTO e mesmo lease" (só o dono do lease conclui);
   - a original ESTORNADA (no estorno) ou CAPTURADA (na captura), condicionada a "APROVADA";
   - os itens `OUTBOX#evento`, com o CloudEvent já serializado (`cloudevents.py`).

   Aqui transação é a escolha certa: são itens desta autorização, sem contenção.
5. **De volta ao HTTP:** 201 (+ `Location`), 200 replay (`Idempotent-Replayed: true`, corpo **idêntico** porque é montado só a partir de campos gravados), 402, 404, 409, 422 ou 503.

## Falhas, e por que nada fica pela metade

| Falha | O que acontece | Arquivo / teste |
|---|---|---|
| `limites` lento ou fora | Timeout → retry com jitter (seguro: idempotente) → circuit breaker → **503** | `limites_http.py`, `disjuntor.py`, `test_disjuntor.py` |
| Processo morre depois de reservar e antes de concluir | Retry do canal (mesma chave) **retoma**: o limite responde JA_APLICADA e a reserva não dobra | `test_retry_depois_de_aplicado_no_limite_mas_nao_concluido_nao_reaplica` |
| Ninguém retoma | **Reconciliador** (`application/reconciliar.py`): RESERVA/DEBITO → compensa numa máquina de estados **gravada passo a passo** (`COMPENSANDO` antes de qualquer chamada, então o cliente não retoma mais; **pergunta** ao limite se a operação está aplicada, sem escrever; se estiver, reversão; `COMPENSADA` com evento); CREDITO, ESTORNO e captura → conclui (roll forward) | `test_reconciliador.py` (inclusive os PoCs do painel: reversão falhando por minutos, cliente tentando retomar durante a compensação) |
| Retomada ou reconciliação depois de 4 min (`PRAZO_DE_REAPLICACAO`) | A guarda de 5 min do limite já pode ter esquecido a operação, então não se reenvia: o cliente recebe 409 `conflito-de-estado` sem `Retry-After`, e o reconciliador **encaminha** a órfã para `RECONCILIACAO_MANUAL` com o motivo gravado (fila humana `RM#`). RESERVA é exceção: o hold é guarda durável e a compensação segue a qualquer tempo. Runbook R7 | `test_retomada_fora_do_prazo_nao_reenvia_ao_limite`, `test_orfa_fora_do_prazo_vai_para_reconciliacao_manual_sem_tocar_o_limite` |
| Reserva passou de 7 dias sem captura nem estorno | **Expirador** (`application/expirar.py::ExpirarReservas`), no mesmo loop do reconciliador, a cada 30 s: acha no GSI esparso `gsi_vencimentos`, libera o hold com id derivado (`id_operacao_de_expiracao`) e grava EXPIRADA + `ReservaExpirada` numa transação condicionada a "ainda APROVADA". Resultados: `EXPIRADA`, `PERDEU_O_HOLD`, `IGNORADA`, `FALHOU` | `test_reserva_vencida_e_expirada_e_o_limite_volta`, `test_expirador_que_cai_no_meio_repete_a_mesma_liberacao`, `test_vencimento_de_reservas.py` |
| Captura e estorno (ou expiração) do mesmo hold ao mesmo tempo | O hold no limite é o árbitro: um vence, os outros → 409 | `test_captura_que_perde_o_hold_e_recusada_e_nao_mexe_na_reserva` |
| Dois estornos da mesma original ao mesmo tempo | Uma operação no limite (id derivado) + CAS na original: um vence, o outro → 409 | `test_dois_estornos_concorrentes_da_mesma_original_estornam_uma_vez` |
| Sobrecarga | Load shedding → 503 imediato (`/saude` isento) | `test_http_publico.py` |

## Perguntas prováveis e onde está a resposta

| Pergunta | Onde |
|---|---|
| "O replay de uma recusa devolve 200?" | Não: devolve o **mesmo** status. `Autorizacao.resultado` guarda o desfecho original (ADR-0006/0011) |
| "Por que o id é determinístico?" | Para não precisar de tabela de idempotência e ter a janela igual à retenção (ADR-0006) |
| "E sem fencing token, o lease é seguro?" | Na escrita do estado, sim: `concluir` e `regravar` são compare-and-set no `lease_ate` gravado, então quem perdeu o lease falha sem efeito. O ponto frágil é outro: entre o prazo de reaplicação (4 min) e a guarda do limite (5 min) há só **60 s** para o desvio somado dos relógios. A AWS cita poucos milissegundos de desvio entre tasks (não é SLA); um teste acopla as constantes (`tests/integracao_bcs`); o alarme de `ClockErrorBound` é evolução (ADR-0006) |
| "Onde o evento é publicado?" | Em lugar nenhum aqui: só gravado no outbox. Quem publica é o `relay/` (ADR-0007) |
| "Como a reserva vira consumo?" | Captura: DEBITO com `id_autorizacao_original` apontando para a RESERVA. A reserva fica CAPTURADA e saem `TransacaoAutorizada` e `ReservaCapturada` (ADR-0020) |
| "E a reserva que ninguém usa?" | O expirador a libera depois de 7 dias (ADR-0020) |
| "Por que 4 min para reaplicar se o lease é de 10 s?" | Cadeia de tempos: timeout HTTP ~2 s ≪ lease 10 s ≪ graça do reconciliador 60 s ≪ prazo de reaplicação 4 min < guarda do limite 5 min (ADR-0006) |
