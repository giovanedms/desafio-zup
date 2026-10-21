# ADR-0020 — Ciclo de vida do hold: captura, estorno e expiração com o limite como árbitro

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-01, RN-02, RN-03, API-07, EVT-01

## Contexto

O enunciado dá quatro tipos de operação (`DEBITO`, `CREDITO`, `RESERVA`, `ESTORNO`) e diz que a reserva bloqueia limite. Ele não diz como o hold termina. Um painel de revisão adversarial do código do BC Limites (juízes independentes instruídos a reprovar; ver `docs/uso-de-ia.md`) reproduziu dois buracos na primeira versão:

- **Não havia captura.** `RESERVAR 1000` e depois `DEBITAR 1000` dava `LIMITE_INSUFICIENTE`. O contorno "liberar e depois debitar" não é atômico e perde a corrida para outra reserva: o estabelecimento fica sem receber por uma compra aprovada.
- **A expiração de 7 dias não tinha motor.** `Autorizacao.expirar` só era chamada em teste. Hold não estornado ficava preso no limite para sempre.

E um terceiro, achado ao desenhar os dois primeiros: **captura, estorno e expiração disputam o mesmo hold.** `LIBERAR_RESERVA` de hold inexistente respondia `JA_APLICADA` (sucesso). Uma captura que chegasse depois de um estorno teria o débito aplicado e seria recusada na conclusão.

## Drivers

1. Não mudar o enum de `tipo_operacao` do enunciado.
2. Captura atômica: o hold vira consumo numa escrita só, sem janela em que o limite fica livre.
3. Exatamente um desfecho por hold (capturado, estornado ou expirado), sob qualquer concorrência.
4. Nada no caminho quente além do que já existe (uma escrita condicional no item do contrato).

## Decisão

1. **Captura = `DEBITO` com `id_autorizacao_original` apontando para uma `RESERVA` aprovada.** É campo aditivo que já existia para o estorno, então o enum do enunciado fica intacto.
   - Captura **parcial** é permitida (até o valor reservado). A sobra volta ao disponível na mesma escrita, como em posto de combustível ou hotel.
   - Não exige contrato ATIVO: o hold foi aprovado quando o contrato estava ativo.
   - Validação no BC Autorização: a original existe no mesmo contrato, é `RESERVA`, está `APROVADA` e cobre o valor (senão 422; já encerrada → 409).
2. **No BC Limites, `CAPTURAR_RESERVA` é uma operação nova** (OCP: subclasse nova, adapters intactos). Plano: pré-condição "o hold `id_reserva` existe com `valor_reservado`"; efeitos `reservado −= valor_reservado`, `utilizado += valor`, remover o hold. O `compilar` genérico já sabia traduzir isso para uma `UpdateItem`.
3. **O hold é o árbitro.** Captura, estorno e expiração viram operações que **removem o hold**. A primeira que remove vence. As outras recebem `RECUSADA` com `RESERVA_NAO_ENCONTRADA`, nunca sucesso silencioso. Só o vencedor atualiza a reserva original (`APROVADA → CAPTURADA | ESTORNADA | EXPIRADA`, CAS "ainda APROVADA, ou na fila humana por falta de hold"), então os dois árbitros (limite e autorização) nunca discordam.
   - Prova: 20 capturas + 10 liberações concorrentes do mesmo hold → exatamente 1 `APLICADA`, na suíte de contrato da porta (memória e DynamoDB/LocalStack). Na demo, pela API pública: 10 capturas × 10 estornos → `{201: 1, 409: 19}`.
4. **Expirador de reservas** no mesmo processo do reconciliador (ECS, Service Connect para chamar o `limites`), a cada 30 s:
   - acha as vencidas num **GSI esparso** `gsi_vencimentos` (`venc_shard` RV#0..9, `venc_ms`). Só reserva aprovada e pendente tem esses atributos, e o encerramento os remove. Mesmo desenho do `gsi_orfas` (ADR-0006), sem Scan;
   - libera o hold com id **derivado da reserva** (`id_operacao_de_expiracao`). O expirador que cair no meio repete a mesma operação e a guarda do limite absorve;
   - grava `EXPIRADA` + `ReservaExpirada` no outbox numa transação condicionada a "ainda APROVADA".
5. **Eventos e contabilidade.** A captura publica dois fatos: `TransacaoAutorizada` (tipo `DEBITO`, com `id_autorizacao_original`, campo opcional novo) e **`ReservaCapturada`** (schema v1 novo). A contabilização lança o débito pelo primeiro e a baixa da compensação, pelo valor reservado inteiro, pelo segundo. Um evento, um lançamento.

## Consequências

- **Positivas:**
  - O ciclo de vida que o contrato publica (hold de 7 dias, `ReservaExpirada`) agora tem motor e testes.
  - A corrida entre os três desfechos é resolvida por uma escrita condicional que já existia, sem lock nem transação nova.
  - O enum da API pública não mudou.
- **Negativas / custos aceitos:**
  - "DEBITO com original = captura" sobrecarrega o significado de `DEBITO`. Está documentado no OpenAPI e na linguagem ubíqua.
  - Captura que perde para um estorno concorrente termina **recusada** (409) com o hold já estornado. É o comportamento correto (o hold não existe mais), mas o canal precisa tratar 409 em captura.
  - **Reserva APROVADA sem hold.** Duas causas, e o painel de juízes apontou que a primeira versão deste ADR só conhecia uma:
    - expiração interrompida (o expirador liberou o hold e caiu antes de gravar `EXPIRADA`). O desfecho na volta **depende do tráfego** no contrato: sem escrita no meio, a guarda não compactou, a liberação volta `JA_APLICADA` e o expirador grava `EXPIRADA` normalmente; com tráfego e mais de 5 min, a guarda esqueceu e a liberação volta `RESERVA_NAO_ENCONTRADA`;
    - captura ou estorno que removeu o hold e foi parar em `RECONCILIACAO_MANUAL` antes de encerrar a original.
  - Nos dois casos o expirador conta `PERDEU_O_HOLD` e, se a reserva continuar `APROVADA` sem hold **10 min** depois do vencimento (`TOLERANCIA_DA_DISPUTA`: a disputa normal se resolve em segundos, e o vencedor mais lento é resolvido ou encaminhado em até ~4 min), a reserva passa a **`RECONCILIACAO_MANUAL`** (motivo `RESERVA_SEM_HOLD`), numa transação com o evento `TransacaoEmReconciliacaoManual`: sai do índice de vencimentos, entra na fila humana (`RM#`) sem TTL e dispara o alarme `reconciliador-encaminhadas`. O status deixa de dizer "aprovada" para um hold que não existe mais, e estorno ou captura NOVOS dela respondem 409. A fila não é um quarto desfecho: se a captura ou o estorno que removeu o hold ainda estava concluindo (o expirador atrasado chegou no meio), ele encerra a reserva assim mesmo, pela 1ª execução, pela retomada ou pelo reconciliador (até ~4 min), e ela sai da fila com `ReservaCapturada`/`TransacaoEstornada` (painel 7, F7-1).
  - Antes das correções dos painéis 3 e 4, ela sangrava uma chamada ao limite a cada 30 s até o TTL de 30 dias (e depois, por uma hora, ainda como `APROVADA`).

## Quando esta decisão deixa de ser boa

- **Captura incremental** (várias capturas parciais do mesmo hold, como em marketplace): o hold precisaria guardar o saldo restante, e a captura deixaria de removê-lo.
- **Volume de reservas vencendo por segundo** maior que o expirador consegue drenar com 1 task (cada expiração custa uma chamada ao `limites` e uma transação): várias tasks são seguras (a liberação usa o mesmo id derivado e o CAS escolhe quem grava; a perdedora conta `IGNORADA`, não `FALHOU`), mas fariam trabalho repetido. O certo é dividir os 10 shards do índice entre as tasks.
- **O canal precisar de `CAPTURA` explícita no enum** (por exemplo, por padrão de mercado da adquirente): vira um valor novo do enum, mudança aditiva na v1 com o mesmo motor por baixo.

## Alternativas descartadas

| Opção | Por que não |
|---|---|
| Liberar o hold e depois debitar | Duas escritas; entre elas, outra reserva pega o limite e a captura de uma compra aprovada falha |
| Novo valor `CAPTURA` no enum agora | Altera o contrato que o enunciado fixou; a semântica cabe no campo aditivo que já existia |
| Expirar com TTL do DynamoDB + Stream | A AWS só garante a exclusão por TTL "em poucos dias", não no instante; e o hold mora no item do contrato, não num item próprio |
| Um agendamento (EventBridge Scheduler) por reserva | Uma criação de schedule por RESERVA no caminho quente, a 5.000 TPS, com cotas de conta para administrar |
| `LIBERAR_RESERVA` de hold ausente responder `JA_APLICADA` (como antes) | Esconde quem venceu a disputa: captura aplicada e conclusão recusada, ou o contrário |

## Referências

- Painel de revisão adversarial do código do BC Limites (registro fora do ZIP; os achados estão descritos no Contexto deste ADR)
- DynamoDB TTL, prazo de exclusão: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/howitworks-ttl.html
- Código: `services/limites/src/limites/domain/operacoes.py::CapturarReserva`, `limite.py::diagnosticar`, `services/autorizacao/src/autorizacao/application/expirar.py`
