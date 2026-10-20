"""Exaustividade do `compilar`: todo campo de `Precondicoes` e de `Efeitos` muda a escrita no DynamoDB.

O vocabulário do Plano é interpretado em memória (`Limite.atende`/`aplicar`) e no adapter (`compilar`). Um
campo novo esquecido no `compilar` faria a memória recusar e o DynamoDB aceitar, em silêncio. Aqui cada
campo é ligado sozinho, a partir de `dataclasses.fields`, então um campo novo entra no teste sem ninguém
lembrar dele: pré-condição tem de mudar a ConditionExpression; efeito, a UpdateExpression.
"""

from __future__ import annotations

from dataclasses import Field, fields
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from limites.adapters.outbound.dynamodb import compilar
from limites.domain.operacoes import Efeitos, Plano, Precondicoes

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)

# Valor diferente do default para cada tipo anotado. Tipo novo sem amostra falha de propósito.
_AMOSTRAS: dict[str, object] = {
    "bool": True,
    "Decimal": Decimal("10.00"),
    "Decimal | None": Decimal("10.00"),
    "str | None": "res-1",
    "tuple[str, Decimal] | None": ("res-1", Decimal("10.00")),
}

BASE = compilar(Plano("op-1", Precondicoes(), Efeitos()), AGORA)


def _amostra(campo: Field[object]) -> object:
    if not isinstance(campo.type, str) or campo.type not in _AMOSTRAS:
        pytest.fail(
            f"{campo.name}: tipo {campo.type!r} sem amostra; acrescente e confira o `compilar`"
        )
    return _AMOSTRAS[campo.type]


@pytest.mark.parametrize("campo", fields(Precondicoes), ids=lambda f: f.name)
def test_toda_precondicao_vira_condicao_na_escrita(campo: Field[object]) -> None:
    precondicoes = Precondicoes(**{campo.name: _amostra(campo)})

    expressao = compilar(Plano("op-1", precondicoes, Efeitos()), AGORA)

    assert expressao["ConditionExpression"] != BASE["ConditionExpression"], (
        f"Precondicoes.{campo.name} não é tratada pelo compilar: o DynamoDB aceitaria o que a memória recusa"
    )


@pytest.mark.parametrize("campo", fields(Efeitos), ids=lambda f: f.name)
def test_todo_efeito_vira_mudanca_na_escrita(campo: Field[object]) -> None:
    efeitos = Efeitos(**{campo.name: _amostra(campo)})

    expressao = compilar(Plano("op-1", Precondicoes(), efeitos), AGORA)

    assert expressao["UpdateExpression"] != BASE["UpdateExpression"], (
        f"Efeitos.{campo.name} não é tratado pelo compilar: a memória mudaria o saldo e o DynamoDB não"
    )
