"""Comportamento do agregado Limite em exemplos legíveis (um cenário por teste)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from limites.domain.dinheiro import ValorMonetarioInvalido, dinheiro
from limites.domain.limite import (
    JANELA_DE_DEDUPLICACAO,
    Limite,
    PrecondicaoNaoAtendida,
    StatusContrato,
    TipoProduto,
)
from limites.domain.operacoes import (
    CapturarReserva,
    Creditar,
    Debitar,
    EstornarDebito,
    LiberarReserva,
    Reservar,
)
from limites.domain.resultados import Desfecho, MotivoRecusa

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def limite_de(concedido: str, status: StatusContrato = StatusContrato.ATIVO) -> Limite:
    return Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, Decimal(concedido), status=status)


def test_reserva_compromete_limite_sem_consumir() -> None:
    limite = limite_de("1000.00").aplicar(Reservar("r1", Decimal("150.00")).planejar(), AGORA)

    assert limite.saldos.saldo_reservado == Decimal("150.00")
    assert limite.saldos.limite_utilizado == Decimal("0.00")
    assert limite.saldos.limite_disponivel == Decimal("850.00")
    assert limite.versao == 2


def test_debito_consome_limite() -> None:
    limite = limite_de("1000.00").aplicar(Debitar("d1", Decimal("300.00")).planejar(), AGORA)

    assert limite.utilizado == Decimal("300.00")
    assert limite.disponivel == Decimal("700.00")


def test_credito_recompoe_o_utilizado() -> None:
    limite = limite_de("1000.00").aplicar(Debitar("d1", Decimal("300.00")).planejar(), AGORA)

    limite = limite.aplicar(Creditar("c1", Decimal("100.00")).planejar(), AGORA)

    assert limite.utilizado == Decimal("200.00")
    assert limite.disponivel == Decimal("800.00")


def test_liberar_reserva_devolve_o_disponivel() -> None:
    limite = limite_de("1000.00").aplicar(Reservar("r1", Decimal("150.00")).planejar(), AGORA)

    limite = limite.aplicar(LiberarReserva("e1", "r1", Decimal("150.00")).planejar(), AGORA)

    assert limite.reservado == Decimal("0.00")
    assert limite.disponivel == Decimal("1000.00")
    assert "r1" not in limite.reservas


def test_estornar_debito_devolve_o_disponivel() -> None:
    limite = limite_de("1000.00").aplicar(Debitar("d1", Decimal("300.00")).planejar(), AGORA)

    limite = limite.aplicar(EstornarDebito("e1", Decimal("300.00")).planejar(), AGORA)

    assert limite.disponivel == Decimal("1000.00")


def test_reserva_acima_do_disponivel_e_recusada_por_limite_insuficiente() -> None:
    limite = limite_de("100.00")
    reserva = Reservar("r1", Decimal("100.01"))

    assert not limite.atende(reserva.planejar())
    assert limite.diagnosticar(reserva) == (Desfecho.RECUSADA, MotivoRecusa.LIMITE_INSUFICIENTE)


def test_reserva_exatamente_igual_ao_disponivel_e_aceita() -> None:
    limite = limite_de("100.00").aplicar(Reservar("r1", Decimal("100.00")).planejar(), AGORA)

    assert limite.disponivel == Decimal("0.00")


@pytest.mark.parametrize("status", [StatusContrato.BLOQUEADO, StatusContrato.ENCERRADO])
def test_contrato_nao_ativo_recusa_reserva_e_debito(status: StatusContrato) -> None:
    limite = limite_de("1000.00", status)

    for operacao in (Reservar("r1", Decimal("1.00")), Debitar("d1", Decimal("1.00"))):
        assert limite.diagnosticar(operacao) == (Desfecho.RECUSADA, MotivoRecusa.CONTRATO_INATIVO)


def test_credito_vale_mesmo_com_contrato_bloqueado() -> None:
    ativo = limite_de("1000.00").aplicar(Debitar("d1", Decimal("300.00")).planejar(), AGORA)
    bloqueado = Limite(**{**_campos(ativo), "status": StatusContrato.BLOQUEADO})

    assert bloqueado.atende(Creditar("c1", Decimal("300.00")).planejar())


def test_credito_maior_que_o_utilizado_e_recusado() -> None:
    limite = limite_de("1000.00").aplicar(Debitar("d1", Decimal("100.00")).planejar(), AGORA)

    assert limite.diagnosticar(Creditar("c1", Decimal("100.01"))) == (
        Desfecho.RECUSADA,
        MotivoRecusa.CREDITO_EXCEDE_UTILIZADO,
    )


def test_mesma_operacao_duas_vezes_e_ja_aplicada_e_nao_muda_o_estado() -> None:
    debito = Debitar("d1", Decimal("100.00"))
    limite = limite_de("1000.00").aplicar(debito.planejar(), AGORA)

    assert limite.diagnosticar(debito) == (Desfecho.JA_APLICADA, None)
    with pytest.raises(PrecondicaoNaoAtendida):
        limite.aplicar(debito.planejar(), AGORA)


def test_reservar_repetido_depois_da_compactacao_continua_idempotente() -> None:
    reserva = Reservar("r1", Decimal("100.00"))
    limite = limite_de("1000.00").aplicar(reserva.planejar(), AGORA)

    compactado = limite.compactar(AGORA + JANELA_DE_DEDUPLICACAO + timedelta(seconds=1))

    assert not compactado.ja_aplicou("r1")
    assert compactado.diagnosticar(reserva) == (Desfecho.JA_APLICADA, None)


def test_liberar_reserva_inexistente_e_recusada_e_nao_sucesso_silencioso() -> None:
    assert limite_de("1000.00").diagnosticar(LiberarReserva("e1", "r-x", Decimal("10.00"))) == (
        Desfecho.RECUSADA,
        MotivoRecusa.RESERVA_NAO_ENCONTRADA,
    )


def test_liberar_repetido_depois_de_aplicado_continua_ja_aplicada() -> None:
    liberar = LiberarReserva("e1", "r1", Decimal("150.00"))
    limite = (
        limite_de("1000.00")
        .aplicar(Reservar("r1", Decimal("150.00")).planejar(), AGORA)
        .aplicar(liberar.planejar(), AGORA)
    )

    assert limite.diagnosticar(liberar) == (Desfecho.JA_APLICADA, None)


def test_captura_maior_que_o_hold_e_invalida() -> None:
    with pytest.raises(ValorMonetarioInvalido):
        CapturarReserva("c1", "r1", Decimal("100.00"), Decimal("100.01"))


def test_liberar_com_valor_diferente_do_hold_e_recusado() -> None:
    limite = limite_de("1000.00").aplicar(Reservar("r1", Decimal("150.00")).planejar(), AGORA)

    assert limite.diagnosticar(LiberarReserva("e1", "r1", Decimal("15.00"))) == (
        Desfecho.RECUSADA,
        MotivoRecusa.RESERVA_DIVERGENTE,
    )


def test_compactar_remove_so_operacoes_fora_da_janela() -> None:
    limite = limite_de("1000.00").aplicar(Debitar("antiga", Decimal("1.00")).planejar(), AGORA)
    depois = AGORA + JANELA_DE_DEDUPLICACAO + timedelta(seconds=1)
    limite = limite.aplicar(Debitar("recente", Decimal("1.00")).planejar(), depois)

    compactado = limite.compactar(depois)

    assert set(compactado.operacoes_recentes) == {"recente"}


@pytest.mark.parametrize("valor", ["0", "-1.00", "1.001", "10000000000000000.00"])
def test_valor_invalido_e_rejeitado_na_criacao_da_operacao(valor: str) -> None:
    with pytest.raises(ValorMonetarioInvalido):
        Reservar("r1", Decimal(valor))


def test_float_nunca_entra_como_dinheiro() -> None:
    with pytest.raises(ValorMonetarioInvalido):
        dinheiro(0.1)  # type: ignore[arg-type]


def test_dinheiro_normaliza_para_duas_casas() -> None:
    assert str(dinheiro("19.9")) == "19.90"


def _campos(limite: Limite) -> dict[str, object]:
    return {nome: getattr(limite, nome) for nome in limite.__dataclass_fields__}
