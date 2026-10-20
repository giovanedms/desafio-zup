"""Anti-corruption layer de entrada: corpo de mensagem SQS (envelope do EventBridge) → CloudEvent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from contabilizacao.domain.regras import EventoInvalido


def cloudevent_da_mensagem(corpo: str) -> Mapping[str, Any]:
    """O EventBridge entrega `{"version","id","detail-type","source","time","detail":{...}}`; o
    `detail` é o CloudEvent. `parse_float=Decimal` preserva o número exato do texto (dinheiro nunca
    passa por float)."""
    try:
        envelope = json.loads(corpo, parse_float=Decimal)
    except (TypeError, ValueError) as erro:
        raise EventoInvalido(f"corpo da mensagem não é JSON: {erro}") from erro
    detail = envelope.get("detail") if isinstance(envelope, dict) else None
    if not isinstance(detail, dict):
        raise EventoInvalido("envelope do EventBridge sem 'detail' objeto")
    return detail
