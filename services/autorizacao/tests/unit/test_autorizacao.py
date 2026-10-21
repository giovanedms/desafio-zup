"""Máquina de estados, eventos de domínio e idempotência do agregado Autorização."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    PRAZO_DE_REAPLICACAO,
    VALIDADE_DA_RESERVA,
    Autorizacao,
    MotivoDaReconciliacao,
    MotivoRecusa,
    Resultado,
    StatusAutorizacao,
    TransicaoInvalida,
)
from autorizacao.domain.eventos import (
    ReservaExpirada,
    SaldosDoLimite,
    TransacaoAutorizada,
    TransacaoEmReconciliacaoManual,
    TransacaoEstornada,
    TransacaoRecusada,
)
from autorizacao.domain.idempotencia import (
    EscopoDeIdempotencia,
    Reexecucao,
    avaliar_reexecucao,
    impressao_digital,
)
from autorizacao.domain.pedido import PedidoDeAutorizacao, PedidoInvalido, TipoOperacao
from hypothesis import given
from hypothesis import strategies as st

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
SALDOS = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 2
)
CHAVE = "3f2b6a1e-8c4d-4e5f-9a0b-1c2d3e4f5a6b"


def pedido(
    tipo: TipoOperacao = TipoOperacao.RESERVA, valor: str = "150.00", **extra: object
) -> PedidoDeAutorizacao:
    return PedidoDeAutorizacao(
        id_contrato="CTR-1",
        id_conta="0001-1",
        valor=Decimal(valor),
        moeda="BRL",
        tipo=tipo,
        **extra,  # type: ignore[arg-type]
    )


def iniciada(p: PedidoDeAutorizacao | None = None, agora: datetime = AGORA) -> Autorizacao:
    p = p or pedido()
    return Autorizacao.iniciar(
        p,
        id_autorizacao="aut-1",
        impressao_digital=impressao_digital(p),
        client_id="canal-pos",
        agora=agora,
        correlation_id="corr-1",
        traceparent=None,
    )


# --- Pedido -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valor", "campo"),
    [("0", "valor"), ("-1", "valor"), ("1.001", "valor"), ("10000000000000000.00", "valor")],
)
def test_pedido_rejeita_valor_invalido(valor: str, campo: str) -> None:
    with pytest.raises(PedidoInvalido) as erro:
        pedido(valor=valor)
    assert erro.value.campo == campo


def test_pedido_so_aceita_brl() -> None:
    with pytest.raises(PedidoInvalido, match="BRL"):
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("1.00"), "USD", TipoOperacao.DEBITO)


def test_estorno_exige_autorizacao_original() -> None:
    with pytest.raises(PedidoInvalido) as erro:
        pedido(TipoOperacao.ESTORNO)
    assert erro.value.campo == "id_autorizacao_original"


@pytest.mark.parametrize("tipo", [TipoOperacao.RESERVA, TipoOperacao.CREDITO])
def test_autorizacao_original_so_em_estorno_ou_captura(tipo: TipoOperacao) -> None:
    with pytest.raises(PedidoInvalido):
        pedido(tipo, id_autorizacao_original="aut-0")


def test_debito_com_original_e_captura() -> None:
    assert pedido(TipoOperacao.DEBITO, id_autorizacao_original="aut-0").eh_captura
    assert not pedido(TipoOperacao.DEBITO).eh_captura


# --- Idempotência ----------------------------------------------------------------------------


def test_mesmo_escopo_gera_o_mesmo_id_e_contratos_diferentes_nao_colidem() -> None:
    a = EscopoDeIdempotencia("canal-pos", "POST", "/v1/contratos/CTR-1/autorizacoes", CHAVE)
    b = EscopoDeIdempotencia("canal-pos", "post", "/v1/contratos/CTR-1/autorizacoes", CHAVE.upper())
    outro_contrato = EscopoDeIdempotencia(
        "canal-pos", "POST", "/v1/contratos/CTR-2/autorizacoes", CHAVE
    )
    outro_cliente = EscopoDeIdempotencia(
        "canal-app", "POST", "/v1/contratos/CTR-1/autorizacoes", CHAVE
    )

    assert a.id_autorizacao() == b.id_autorizacao()
    assert a.id_autorizacao() != outro_contrato.id_autorizacao()
    assert a.id_autorizacao() != outro_cliente.id_autorizacao()


def test_impressao_digital_e_semantica() -> None:
    assert impressao_digital(pedido(valor="150")) == impressao_digital(pedido(valor="150.00"))
    assert impressao_digital(pedido(valor="150.00")) != impressao_digital(pedido(valor="150.01"))


def test_reexecucao_com_pedido_diferente_e_chave_reutilizada() -> None:
    existente = iniciada()
    assert avaliar_reexecucao(existente, "outra", AGORA) is Reexecucao.CHAVE_REUTILIZADA


def test_reexecucao_durante_o_lease_e_em_andamento() -> None:
    existente = iniciada()
    assert (
        avaliar_reexecucao(existente, existente.impressao_digital, AGORA) is Reexecucao.EM_ANDAMENTO
    )


def test_reexecucao_com_lease_vencido_retoma() -> None:
    existente = iniciada()
    depois = AGORA + DURACAO_DO_LEASE + timedelta(milliseconds=1)
    assert avaliar_reexecucao(existente, existente.impressao_digital, depois) is Reexecucao.RETOMAR


def test_reexecucao_de_concluida_e_replay_mesmo_depois_de_estornada() -> None:
    aprovada = iniciada().aprovar(SALDOS, AGORA, "ev-1").estornar(AGORA)
    assert aprovada.status is StatusAutorizacao.ESTORNADA
    assert avaliar_reexecucao(aprovada, aprovada.impressao_digital, AGORA) is Reexecucao.REPLAY


def test_reexecucao_de_compensando_ou_compensada_e_encerrada() -> None:
    compensando = iniciada().iniciar_compensacao(AGORA + DURACAO_DO_LEASE + timedelta(seconds=1))
    compensada = compensando.compensar(AGORA + DURACAO_DO_LEASE + timedelta(seconds=2), "ev-c")
    for a in (compensando, compensada):  # o reconciliador já decidiu: o cliente não retoma mais
        assert (
            avaliar_reexecucao(a, a.impressao_digital, AGORA + timedelta(minutes=1))
            is Reexecucao.ENCERRADA_SEM_RESULTADO
        )


# --- Máquina de estados e eventos ------------------------------------------------------------


def test_aprovar_registra_resultado_saldos_e_evento() -> None:
    aprovada = iniciada().aprovar(SALDOS, AGORA, "ev-1")

    assert aprovada.status is StatusAutorizacao.APROVADA
    assert aprovada.resultado is Resultado.APROVADA
    assert aprovada.saldos == SALDOS
    (evento,) = aprovada.eventos
    assert isinstance(evento, TransacaoAutorizada)
    assert (evento.id_evento, evento.valor, evento.saldos.saldo_reservado) == (
        "ev-1",
        Decimal("150.00"),
        Decimal("150.00"),
    )


def test_recusar_registra_motivo_e_evento() -> None:
    recusada = iniciada().recusar(MotivoRecusa.LIMITE_INSUFICIENTE, SALDOS, AGORA, "ev-1")

    assert (recusada.status, recusada.resultado, recusada.motivo_recusa) == (
        StatusAutorizacao.RECUSADA,
        Resultado.RECUSADA,
        MotivoRecusa.LIMITE_INSUFICIENTE,
    )
    assert isinstance(recusada.eventos[0], TransacaoRecusada)


def test_estorno_aprovado_emite_transacao_estornada_com_a_original() -> None:
    original = iniciada().aprovar(SALDOS, AGORA, "ev-1")
    estorno = iniciada(pedido(TipoOperacao.ESTORNO, id_autorizacao_original="aut-1"))

    aprovado = estorno.aprovar(SALDOS, AGORA, "ev-2", original=original)

    (evento,) = aprovado.eventos
    assert isinstance(evento, TransacaoEstornada)
    assert (evento.id_autorizacao_original, evento.tipo_operacao_original) == ("aut-1", "RESERVA")


def test_estorno_sem_original_e_transicao_invalida() -> None:
    estorno = iniciada(pedido(TipoOperacao.ESTORNO, id_autorizacao_original="aut-1"))
    with pytest.raises(TransicaoInvalida):
        estorno.aprovar(SALDOS, AGORA, "ev-2")


@pytest.mark.parametrize("tipo", [TipoOperacao.CREDITO])
def test_so_reserva_e_debito_sao_estornaveis(tipo: TipoOperacao) -> None:
    aprovada = iniciada(pedido(tipo)).aprovar(SALDOS, AGORA, "ev-1")
    with pytest.raises(TransicaoInvalida):
        aprovada.estornar(AGORA)


def test_nao_se_estorna_duas_vezes() -> None:
    estornada = iniciada().aprovar(SALDOS, AGORA, "ev-1").estornar(AGORA)
    with pytest.raises(TransicaoInvalida):
        estornada.estornar(AGORA)


def test_reserva_expira_so_depois_da_validade() -> None:
    aprovada = iniciada().aprovar(SALDOS, AGORA, "ev-1")

    with pytest.raises(TransicaoInvalida):
        aprovada.expirar(SALDOS, AGORA + VALIDADE_DA_RESERVA - timedelta(seconds=1), "ev-2")

    expirada = aprovada.sem_eventos().expirar(SALDOS, AGORA + VALIDADE_DA_RESERVA, "ev-2")
    assert expirada.status is StatusAutorizacao.EXPIRADA
    assert isinstance(expirada.eventos[0], ReservaExpirada)


def test_debito_nao_expira() -> None:
    aprovada = iniciada(pedido(TipoOperacao.DEBITO)).aprovar(SALDOS, AGORA, "ev-1")
    assert aprovada.expira_em is None


def test_compensar_so_depois_de_iniciar_a_compensacao() -> None:
    aprovada = iniciada().aprovar(SALDOS, AGORA, "ev-1")
    with pytest.raises(TransicaoInvalida):
        aprovada.compensar(AGORA, "ev-c")
    with pytest.raises(
        TransicaoInvalida
    ):  # sem COMPENSANDO gravado, o cliente ainda poderia retomar
        iniciada().compensar(AGORA, "ev-c")
    assert iniciada().iniciar_compensacao(AGORA).compensar(AGORA, "ev-c").status is (
        StatusAutorizacao.COMPENSADA
    )


def test_progresso_da_compensacao_renova_o_lease_e_e_preservado() -> None:
    t1, t2, t3 = (AGORA + timedelta(seconds=s) for s in (70, 71, 72))
    compensando = iniciada().iniciar_compensacao(t1)
    garantida = compensando.confirmar_aplicacao(t2)
    desfazendo = garantida.marcar_desfazer(t3).marcar_desfazer(t3 + timedelta(minutes=1))

    assert compensando.lease_ate == t1 + DURACAO_DO_LEASE
    assert garantida.aplicacao_confirmada_em == t2
    assert desfazendo.desfazer_desde == t3  # a 1ª tentativa não muda: o prazo conta dela
    assert desfazendo.pode_desfazer(t3 + PRAZO_DE_REAPLICACAO)
    assert not desfazendo.pode_desfazer(t3 + PRAZO_DE_REAPLICACAO + timedelta(seconds=1))
    with pytest.raises(TransicaoInvalida):
        compensando.marcar_desfazer(t2)  # só desfaz o que está garantido


@pytest.mark.parametrize("tipo", [TipoOperacao.CREDITO, TipoOperacao.ESTORNO])
def test_roll_forward_nao_compensa(tipo: TipoOperacao) -> None:
    extra = {"id_autorizacao_original": "aut-0"} if tipo is TipoOperacao.ESTORNO else {}
    with pytest.raises(TransicaoInvalida):
        iniciada(pedido(tipo, **extra)).iniciar_compensacao(AGORA)


def test_encaminhar_guarda_motivo_e_progresso() -> None:
    garantida = iniciada().iniciar_compensacao(AGORA).confirmar_aplicacao(AGORA)

    manual = garantida.encaminhar_para_reconciliacao(
        MotivoDaReconciliacao.DESFAZER_RECUSADO, AGORA, "ev-m"
    )

    assert manual.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    assert manual.motivo_da_reconciliacao is MotivoDaReconciliacao.DESFAZER_RECUSADO
    assert manual.aplicacao_confirmada_em == AGORA
    (evento,) = manual.eventos  # trilha durável do caso de efeito incerto
    assert isinstance(evento, TransacaoEmReconciliacaoManual)
    assert evento.motivo == "DESFAZER_RECUSADO" and evento.aplicacao_confirmada_em == AGORA


def test_reserva_na_fila_sem_hold_ainda_e_encerrada_pelo_vencedor_e_sai_da_fila() -> None:
    """F7-1 (painel 7): o expirador encaminha a reserva por falta de hold enquanto a captura/estorno que o
    removeu ainda não concluiu. O vencedor encerra assim mesmo; o motivo da fila deixa de valer."""
    vencida = AGORA + VALIDADE_DA_RESERVA + timedelta(minutes=11)
    na_fila = (
        iniciada().aprovar(SALDOS, AGORA, "ev-1").encaminhar_reserva_sem_hold(vencida, "ev-rm")
    )

    assert na_fila.encerravel_pelo_vencedor
    capturada = na_fila.capturar(vencida)
    estornada = na_fila.estornar(vencida)
    assert capturada.status is StatusAutorizacao.CAPTURADA
    assert estornada.status is StatusAutorizacao.ESTORNADA
    assert capturada.motivo_da_reconciliacao is estornada.motivo_da_reconciliacao is None


def test_fila_humana_por_outro_motivo_nao_e_encerrada_por_captura_nem_estorno() -> None:
    orfa = iniciada().encaminhar_para_reconciliacao(
        MotivoDaReconciliacao.FORA_DO_PRAZO, AGORA, "ev-m"
    )

    assert not orfa.encerravel_pelo_vencedor
    with pytest.raises(TransicaoInvalida):
        orfa.capturar(AGORA)
    with pytest.raises(TransicaoInvalida):
        orfa.estornar(AGORA)


def test_assumir_renova_lease_so_se_vencido() -> None:
    depois = AGORA + DURACAO_DO_LEASE + timedelta(seconds=1)
    assumida = iniciada().assumir(depois)
    assert assumida.lease_ate == depois + DURACAO_DO_LEASE
    with pytest.raises(TransicaoInvalida):
        assumida.assumir(depois)


@given(
    acoes=st.lists(
        st.sampled_from(["aprovar", "recusar", "estornar", "compensar", "expirar"]), max_size=8
    )
)
def test_concluida_nunca_volta_a_em_processamento_e_resultado_nunca_muda(acoes: list[str]) -> None:
    autorizacao = iniciada()
    resultado_fixado: Resultado | None = None
    instante = AGORA + VALIDADE_DA_RESERVA + DURACAO_DO_LEASE
    for acao in acoes:
        try:
            if acao == "aprovar":
                autorizacao = autorizacao.aprovar(SALDOS, instante, "ev")
            elif acao == "recusar":
                autorizacao = autorizacao.recusar(
                    MotivoRecusa.LIMITE_INSUFICIENTE, SALDOS, instante, "ev"
                )
            elif acao == "estornar":
                autorizacao = autorizacao.estornar(instante)
            elif acao == "compensar":
                autorizacao = autorizacao.iniciar_compensacao(instante).compensar(instante, "ev")
            else:
                autorizacao = autorizacao.expirar(SALDOS, instante, "ev")
        except TransicaoInvalida:
            continue
        if resultado_fixado is not None:
            assert autorizacao.resultado is resultado_fixado
            assert autorizacao.status is not StatusAutorizacao.EM_PROCESSAMENTO
        resultado_fixado = autorizacao.resultado or resultado_fixado
