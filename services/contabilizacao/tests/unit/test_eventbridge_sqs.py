"""Envelope do EventBridge → CloudEvent, sem perder a precisão do número."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from contabilizacao.adapters.inbound.eventbridge_sqs import cloudevent_da_mensagem
from contabilizacao.domain.regras import EventoInvalido


def envelope(detail_json: str) -> str:
    return (
        '{"version":"0","id":"abc","detail-type":"TransacaoAutorizada",'
        '"source":"financas.autorizacao","time":"2026-10-20T13:45:12Z","detail":'
        + detail_json
        + "}"
    )


def test_devolve_o_detail() -> None:
    ce = cloudevent_da_mensagem(envelope('{"id":"e1","data":{"valor":150.0}}'))
    assert ce["id"] == "e1"
    assert ce["data"]["valor"] == Decimal("150.0")


def test_numero_grande_e_exato() -> None:
    ce = cloudevent_da_mensagem(envelope('{"data":{"valor":12345678901234.56}}'))
    valor = ce["data"]["valor"]
    assert isinstance(valor, Decimal)
    assert valor == Decimal("12345678901234.56")  # com float seria 12345678901234.559


@pytest.mark.parametrize(
    "corpo",
    ["", "não é json", "[1, 2]", "null", json.dumps({"version": "0"}), envelope('"texto"')],
)
def test_corpo_malformado_e_evento_invalido(corpo: str) -> None:
    with pytest.raises(EventoInvalido):
        cloudevent_da_mensagem(corpo)
