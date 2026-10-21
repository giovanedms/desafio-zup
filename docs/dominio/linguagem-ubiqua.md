# Linguagem ubíqua

Os termos abaixo são os mesmos no enunciado, nos contratos (OpenAPI e eventos), no código e na conversa com o negócio.

## BC Autorização de Transações

| Termo | Definição | No código |
|---|---|---|
| **Autorização** | Decisão (aprovar ou recusar) sobre um pedido de transação contra o limite de um contrato. Tem ciclo de vida | `autorizacao/domain/autorizacao.py::Autorizacao` |
| **Pedido de autorização** | O comando vindo do canal: contrato, conta, valor, moeda, tipo de operação | `domain/pedido.py::PedidoDeAutorizacao` |
| **Tipo de operação** | RESERVA (hold), DEBITO (consumo, ou captura quando referencia uma RESERVA), CREDITO (recomposição, ex.: pagamento), ESTORNO (reversão de uma autorização anterior) | `TipoOperacao` |
| **Resultado** | O desfecho original (APROVADA/RECUSADA), que o replay devolve | `Autorizacao.resultado` |
| **Status** | Estado atual: EM_PROCESSAMENTO, APROVADA, RECUSADA, ESTORNADA, CAPTURADA, EXPIRADA, COMPENSADA, RECONCILIACAO_MANUAL | `StatusAutorizacao` |
| **Recusa** | Resultado de negócio (não é erro): limite insuficiente, contrato inativo... Fica registrada e é publicada | `MotivoRecusa` |
| **Chave de idempotência** | `Idempotency-Key` do canal. Com cliente, método e URI forma o **escopo** que gera o id da autorização | `domain/idempotencia.py` |
| **Impressão digital** | Hash do conteúdo semântico do pedido; detecta a mesma chave com pedido diferente | `impressao_digital` |
| **Lease** | Janela (10 s) em que uma execução é dona da autorização EM_PROCESSAMENTO | `lease_ate` |
| **Retomada** | Retry do canal depois do lease vencido; continua a mesma autorização | `Reexecucao.RETOMAR` |
| **Órfã** | Autorização EM_PROCESSAMENTO cujo lease venceu sem retomada | `application/reconciliar.py` |
| **Compensação** | Desfazer o efeito no limite de uma órfã (RESERVA/DEBITO), numa máquina de estados gravada: `COMPENSANDO` (o cliente não retoma mais) → pergunta ao limite (leitura) → aplicação confirmada → reversão → `COMPENSADA`. CREDITO, ESTORNO e captura não compensam: concluem (roll forward) | `ReconciliarOrfas`, `iniciar_compensacao`, `confirmar_aplicacao`, `marcar_desfazer`, `GatewayDeLimites.consultar_operacao` |
| **Prazo de reaplicação** | 4 min a partir de `criada_em`. Até ele, a retomada pode reenviar a operação ao limite (a guarda de 5 min ainda a reconhece). Depois, 409 `conflito-de-estado` sem `Retry-After`, sem chamar o limite. A reversão da compensação tem o mesmo prazo, contado da 1ª tentativa. RESERVA não depende dele (o hold é guarda durável) | `PRAZO_DE_REAPLICACAO`, `pode_desfazer` |
| **Reconciliação manual** | Status de quem não dá para concluir sozinho com segurança (motivos: `FORA_DO_PRAZO`, `DESFAZER_FORA_DO_PRAZO`, `DESFAZER_RECUSADO`; reserva: `RESERVA_SEM_HOLD`). Vai para a **fila humana** (`RM#` no `gsi_orfas`), com alarme, e uma pessoa decide (runbook R7) | `StatusAutorizacao.RECONCILIACAO_MANUAL`, `MotivoDaReconciliacao`, `encaminhar_para_reconciliacao` |
| **Estorno** | Autorização do tipo ESTORNO que reverte uma original aprovada; a original fica ESTORNADA | `Autorizacao.estornar` |
| **Captura** | DEBITO com `id_autorizacao_original` apontando para uma RESERVA aprovada: o hold vira consumo. Pode ser parcial (valor ≤ reservado; a sobra volta ao disponível). A reserva original fica CAPTURADA (ADR-0020) | `Autorizacao.capturar` |
| **Expiração** | Reserva aprovada não capturada nem estornada em 7 dias é liberada pelo expirador (a cada 30 s, junto do reconciliador); a reserva fica EXPIRADA | `Autorizacao.expirar`, `application/expirar.py::ExpirarReservas` |

## BC Gestão de Limites

| Termo | Definição | No código |
|---|---|---|
| **Limite** | Saldo de crédito de um contrato; agregado com as invariantes I1–I6 | `limites/domain/limite.py::Limite` |
| **Limite concedido** | Teto definido pelo crédito no core legado (dono: legado) | `concedido` |
| **Utilizado** | **Saldo devedor líquido** do contrato: débitos e capturas somam; créditos (pagamento) e estornos abatem, de qualquer débito e não de um débito específico. Não existe saldo credor: crédito ou estorno maior que o utilizado é recusado (`CREDITO_EXCEDE_UTILIZADO`). Exemplo: compra 100 → pagamento 100 → compra 100 → estorno da 1ª compra = 100 − 100 + 100 − 100 = 0, que é o que o limite deve mostrar (não é vazamento) | `utilizado` |
| **Reservado / saldo reservado** | Soma das reservas (holds) ativas — o `saldo_reservado` do enunciado | `reservado` |
| **Disponível** | Concedido − utilizado − reservado | `disponivel` |
| **Reserva (hold)** | Valor comprometido sem consumo, identificado pelo id da autorização | `reservas` |
| **Operação** | Comando sobre o limite: reservar, debitar, creditar, liberar reserva, capturar reserva, estornar débito | `domain/operacoes.py` |
| **Captura de reserva** | `CAPTURAR_RESERVA`: numa escrita só, `reservado −= valor_reservado`, `utilizado += valor` e remoção do hold | `CapturarReserva` |
| **Árbitro do hold** | O hold decide a disputa: captura, estorno e expiração removem o hold e a primeira que o remove vence. As outras são RECUSADAS com `RESERVA_NAO_ENCONTRADA` (409 `conflito-de-estado` no BC Autorização) | ADR-0020; `Limite.diagnosticar` |
| **Guarda de idempotência** | Mapa `ops` no item do contrato com os ids das operações aplicadas nos últimos 5 min. Faz o retry depois de timeout virar JA_APLICADA. Entradas vencidas saem no `REMOVE` da escrita seguinte do mesmo contrato, sem escrita extra; cada `REMOVE` só apaga entrada ainda vencida (ou já removida), nunca uma reaplicada por outra task dentro da janela. Recusa não grava nada na guarda | `JANELA_DE_DEDUPLICACAO`, `operacoes_expiradas`, `compilar` |
| **Plano** | Pré-condições + efeitos de uma operação; o adapter o compila numa escrita atômica | `Plano` |
| **Diagnóstico** | Por que uma operação não foi aplicada (já aplicada, ou recusada com motivo) | `Limite.diagnosticar` |
| **Conta divergente** | `CONTA_DIVERGENTE`: recusa da operação cujo `id_conta` (opcional no contrato interno) não é a conta do contrato na réplica do core. Sem efeito nenhum, nem na guarda: o mesmo `id_operacao` com a conta certa ainda é aplicado. A idempotência vem antes (retry de operação já aplicada é JA_APLICADA mesmo se o legado trocou a conta depois), e a conta vem antes das outras recusas (quem não é da conta não fica sabendo de status nem de holds) | `MotivoRecusa.CONTA_DIVERGENTE`, `Precondicoes.id_conta` |
| **Contrato quente** | Contrato com volume de operações próximo do teto por item (~8 op/s sustentadas, porque a guarda mora no item). Observado por `OperacoesNaGuarda` e Contributor Insights | ADR-0016 |
| **Atualização do legado** | Mudança de concedido, status, conta ou produto vinda do core, com versão monotônica. Nunca toca utilizado nem reservado | `domain/legado.py` |

## BC Contabilização

| Termo | Definição | No código |
|---|---|---|
| **Lançamento** | Registro contábil imutável em partidas dobradas originado de um evento | `contabilizacao/domain/lancamento.py` |
| **Partida** | Débito ou crédito numa conta | `Partida` |
| **Data contábil** | Data local (São Paulo) do fato de negócio | `data_contabil` |
| **Inbox** | Registro dos eventos já contabilizados (dedup por `source` + `id`) | adapter DynamoDB |
| **Contas de compensação** | Contas que registram compromissos (limite reservado) fora do balanço | `Conta.COMPENSACAO_*` |
