"""Propriedades do agregado Limite para QUALQUER sequência de operações (Hypothesis).

Simula o que a escrita condicional faz no banco: para cada operação, se o plano é atendido,
aplica; se não, diagnostica. É o mesmo protocolo dos dois adapters do repositório.
"""

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from limites.domain.limite import Limite, StatusContrato, TipoProduto
from limites.domain.operacoes import (
    Creditar,
    Debitar,
    EstornarDebito,
    LiberarReserva,
    OperacaoDeLimite,
    Reservar,
)
from limites.domain.resultados import Desfecho

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)

centavos = st.integers(min_value=1, max_value=200_000).map(lambda c: Decimal(c) / 100)
ids = st.sampled_from([f"op-{i}" for i in range(12)])  # colisões de id de propósito (idempotência)


@st.composite
def operacoes(draw: st.DrawFn) -> OperacaoDeLimite:
    tipo = draw(st.sampled_from(["R", "D", "C", "L", "E"]))
    id_op, valor = draw(ids), draw(centavos)
    if tipo == "R":
        return Reservar(id_op, valor)
    if tipo == "D":
        return Debitar(id_op, valor)
    if tipo == "C":
        return Creditar(id_op, valor)
    if tipo == "L":
        return LiberarReserva(f"lib-{id_op}", draw(ids), valor)
    return EstornarDebito(id_op, valor)


def executar(limite: Limite, operacao: OperacaoDeLimite) -> tuple[Limite, Desfecho]:
    plano = operacao.planejar()
    if limite.atende(plano):
        return limite.aplicar(plano, AGORA), Desfecho.APLICADA
    desfecho, _ = limite.diagnosticar(operacao)
    return limite, desfecho


@settings(max_examples=400)
@given(concedido=centavos, sequencia=st.lists(operacoes(), max_size=60))
def test_invariantes_valem_apos_qualquer_sequencia(
    concedido: Decimal, sequencia: list[OperacaoDeLimite]
) -> None:
    limite = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, concedido)
    versao_anterior = limite.versao

    for operacao in sequencia:
        limite, desfecho = executar(limite, operacao)
        limite.verificar_invariantes()
        assert limite.disponivel >= 0  # nenhum caminho de autorização deixa o saldo negativo
        assert limite.versao == versao_anterior + (1 if desfecho is Desfecho.APLICADA else 0)
        versao_anterior = limite.versao


@settings(max_examples=200)
@given(
    concedido_reais=st.integers(min_value=1, max_value=5_000),
    valor_reais=st.integers(min_value=1, max_value=500),
    tentativas=st.integers(min_value=1, max_value=200),
)
def test_corrida_de_reservas_aprova_exatamente_o_que_cabe(
    concedido_reais: int, valor_reais: int, tentativas: int
) -> None:
    """O cenário da demo 4: N reservas iguais em qualquer ordem aprovam exatamente ⌊L/v⌋."""
    concedido, valor = Decimal(concedido_reais), Decimal(valor_reais)
    limite = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, concedido)

    aprovadas = 0
    for i in range(tentativas):
        limite, desfecho = executar(limite, Reservar(f"r-{i}", valor))
        aprovadas += desfecho is Desfecho.APLICADA

    assert aprovadas == min(tentativas, concedido_reais // valor_reais)
    assert limite.reservado == valor * aprovadas
    assert limite.disponivel >= 0


@given(concedido=centavos, operacao=operacoes())
def test_repetir_a_mesma_operacao_nunca_muda_o_estado(
    concedido: Decimal, operacao: OperacaoDeLimite
) -> None:
    inicial = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, concedido)
    inicial, _ = executar(inicial, Debitar("seed", min(concedido, Decimal("1.00"))))
    uma_vez, _ = executar(inicial, operacao)

    duas_vezes, desfecho = executar(uma_vez, operacao)

    assert duas_vezes == uma_vez
    assert desfecho is not Desfecho.APLICADA


@given(concedido=centavos, valor=centavos)
def test_reservar_e_liberar_volta_ao_disponivel_original(
    concedido: Decimal, valor: Decimal
) -> None:
    limite = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, concedido)
    reservado, desfecho = executar(limite, Reservar("r1", valor))

    liberado, _ = executar(reservado, LiberarReserva("l1", "r1", valor))

    assert liberado.disponivel == limite.disponivel
    assert (desfecho is Desfecho.APLICADA) == (valor <= concedido)


@given(concedido=centavos, sequencia=st.lists(operacoes(), max_size=30))
def test_contrato_bloqueado_nunca_aprova_reserva_nem_debito(
    concedido: Decimal, sequencia: list[OperacaoDeLimite]
) -> None:
    limite = Limite.novo(
        "CTR-1", "0001-1", TipoProduto.CREDITO, concedido, status=StatusContrato.BLOQUEADO
    )
    for operacao in sequencia:
        antes = limite.saldos
        limite, desfecho = executar(limite, operacao)
        if isinstance(operacao, Reservar | Debitar):
            assert desfecho is not Desfecho.APLICADA
            assert limite.saldos == antes
