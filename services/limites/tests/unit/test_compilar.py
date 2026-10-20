"""Compilação do Plano em expressões do DynamoDB (inclui a compactação de carona)."""

from datetime import UTC, datetime
from decimal import Decimal

from limites.adapters.outbound.dynamodb import MAX_REMOCOES_POR_ESCRITA, compilar
from limites.domain.operacoes import Debitar, LiberarReserva

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
