"""Sweeper: republica o que está no outbox há mais que a idade mínima (relay parado, Stream expirado)."""

from __future__ import annotations

import logging
import time
from typing import Any

from relay.lambda_handler import componentes
from relay.nucleo import Outbox, Publicador, ResultadoDoLote, processar

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

IDADE_MINIMA_MS = 15 * 60 * 1000


def varrer(
    outbox: Outbox,
    publicador: Publicador,
    agora_ms: int,
    idade_minima_ms: int = IDADE_MINIMA_MS,
) -> ResultadoDoLote:
    antigos = outbox.listar_antigos(agora_ms - idade_minima_ms)
    return processar(antigos, publicador, outbox)


def handler_agendado(evento: dict[str, Any], contexto: object) -> dict[str, int]:
    """Alvo do EventBridge Scheduler (AWS)."""
    c = componentes()
    resultado = varrer(c.outbox, c.publicador, int(time.time() * 1000))
    log.info(
        '{"msg":"sweeper","publicados":%d,"falhos":%d}',
        len(resultado.publicados),
        len(resultado.falhos),
    )
    return {"publicados": len(resultado.publicados), "falhos": len(resultado.falhos)}
