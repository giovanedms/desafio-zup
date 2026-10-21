"""Compilação do Plano em expressões do DynamoDB (inclui a compactação de carona)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

from hypothesis import given
from hypothesis import strategies as st
from limites.adapters.outbound.dynamodb import (
    MAX_REMOCOES_POR_ESCRITA,
    _corte_da_janela,
    compilar,
)
from limites.domain.limite import Limite, TipoProduto
from limites.domain.operacoes import (
    CapturarReserva,
    Debitar,
    Efeitos,
    LiberarReserva,
    Plano,
    Precondicoes,
)

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def test_debito_sem_vencidas_nao_tem_remove() -> None:
    expr = compilar(Debitar("d1", Decimal("10.00")).planejar(), AGORA)

    assert "REMOVE" not in expr["UpdateExpression"]
    assert (
        "#reservas" not in expr["ExpressionAttributeNames"]
    )  # placeholder não usado = erro no DynamoDB


def test_vencidas_viram_remove_na_mesma_escrita() -> None:
    expr = compilar(Debitar("d1", Decimal("10.00")).planejar(), AGORA, ("velha-1", "velha-2"))

    assert expr["UpdateExpression"].endswith("REMOVE #ops.#x0, #ops.#x1")
    assert expr["ExpressionAttributeNames"]["#x1"] == "velha-2"


def test_cada_remove_de_carona_so_apaga_entrada_vencida_ou_ja_removida() -> None:
    """F-5: o id visto vencido por esta task pode ter sido reaplicado por outra; a condição protege a entrada fresca."""
    expr = compilar(Debitar("d1", Decimal("10.00")).planejar(), AGORA, ("velha-1", "velha-2"))

    for x in ("#x0", "#x1"):
        assert (
            f"(attribute_not_exists(#ops.{x}) OR #ops.{x} < :corte)" in expr["ConditionExpression"]
        )
    assert expr["ExpressionAttributeValues"][":corte"] == {"N": str(_corte_da_janela(AGORA))}


def test_sem_vencidas_nao_declara_o_corte() -> None:
    expr = compilar(Debitar("d1", Decimal("10.00")).planejar(), AGORA, ("d1",))

    assert ":corte" not in expr["ExpressionAttributeValues"]  # valor sobrando = ValidationException
    assert "REMOVE" not in expr["UpdateExpression"]


@given(
    agora=st.datetimes(
        min_value=datetime(2026, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(UTC)
    ),
    deslocamento=st.integers(-3, 3),
)
def test_corte_da_janela_e_a_mesma_regra_do_dominio(agora: datetime, deslocamento: int) -> None:
    """`ts < corte` ⟺ `Limite.operacoes_expiradas` diz vencida, para o segundo inteiro que a guarda grava."""
    corte = _corte_da_janela(agora)
    ts = corte + deslocamento
    limite = replace(
        Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, Decimal("1.00")),
        operacoes_recentes=MappingProxyType({"op": datetime.fromtimestamp(ts, UTC)}),
    )

    assert (ts < corte) == (limite.operacoes_expiradas(agora) == ("op",))


def test_condicao_do_compilar_cabe_no_limite_de_4_kb_do_dynamodb() -> None:
    """Pior caso: toda pré-condição, ids de 36 posições e o máximo de remoções de carona por escrita."""
    uuid = "6f1c2b0e-3d4a-5b6c-8d7e-9f0a1b2c3d4e"
    plano = Plano(
        uuid,
        Precondicoes(
            contrato_ativo=True,
            disponivel_minimo=Decimal("9999999999999999.99"),
            utilizado_minimo=Decimal("9999999999999999.99"),
            reserva_existente=(uuid, Decimal("9999999999999999.99")),
            reserva_inexistente=uuid,
            id_conta="0" * 16,
        ),
        CapturarReserva(uuid, uuid, Decimal("10.00"), Decimal("5.00")).planejar().efeitos,
    )
    vencidas = [f"{i:08d}-3d4a-5b6c-8d7e-9f0a1b2c3d4e" for i in range(MAX_REMOCOES_POR_ESCRITA)]

    expr = compilar(plano, AGORA, vencidas)

    assert len(expr["ConditionExpression"].encode()) < 4096
    assert len(expr["UpdateExpression"].encode()) < 4096


def test_conta_declarada_vira_condicao_na_escrita() -> None:
    expr = compilar(Debitar("d1", Decimal("10.00"), id_conta="0001-1").planejar(), AGORA)

    assert "#conta = :conta" in expr["ConditionExpression"]
    assert expr["ExpressionAttributeNames"]["#conta"] == "id_conta"
    assert expr["ExpressionAttributeValues"][":conta"] == {"S": "0001-1"}


def test_sem_conta_declarada_nao_ha_condicao_de_conta() -> None:
    sem = compilar(Plano("op-1", Precondicoes(), Efeitos()), AGORA)

    assert "#conta" not in sem["ExpressionAttributeNames"]


def test_liberar_reserva_combina_remove_da_reserva_e_das_vencidas() -> None:
    expr = compilar(LiberarReserva("l1", "r1", Decimal("5.00")).planejar(), AGORA, ("velha",))

    assert expr["UpdateExpression"].count("REMOVE") == 1
    assert "#reservas.#rr" in expr["UpdateExpression"]
    assert "#ops.#x0" in expr["UpdateExpression"]


def test_limite_de_remocoes_por_escrita_e_razoavel() -> None:
    assert 10 <= MAX_REMOCOES_POR_ESCRITA <= 100


def test_id_da_propria_operacao_nunca_entra_no_remove() -> None:
    """SET e REMOVE no mesmo caminho é ValidationException no DynamoDB (retry do mesmo id após a janela)."""
    expr = compilar(Debitar("d1", Decimal("10.00")).planejar(), AGORA, ("d1", "velha"))

    assert "#x0" in expr["ExpressionAttributeNames"]
    assert expr["ExpressionAttributeNames"]["#x0"] == "velha"
    assert "d1" not in [
        v for k, v in expr["ExpressionAttributeNames"].items() if k.startswith("#x")
    ]
