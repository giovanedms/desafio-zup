"""Sweeper: republica o que está no outbox há mais que a idade mínima (relay parado, Stream expirado).

Drenagem INCREMENTAL: lê o outbox em páginas de tamanho limitado e publica cada página antes de ler a
próxima. Para no teto de itens ou no orçamento de tempo da rodada (abaixo do timeout da Lambda); o que
sobrar sai nas rodadas seguintes. Nunca carrega a tabela inteira na memória antes de publicar.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relay.lambda_handler import componentes
from relay.nucleo import Outbox, Publicador, processar

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

IDADE_MINIMA_MS = 15 * 60 * 1000
# Por rodada: tempo e memória (ids publicados) limitados; o resto fica para a próxima.
TETO_DE_ITENS = 10_000
# Por rodada: abaixo do timeout de 60 s da Lambda, com folga para publicar a última página.
ORCAMENTO_S = 40.0
MARGEM_S = 15.0  # com contexto Lambda: para de pedir página quando restar menos que isto


@dataclass(frozen=True)
class ResultadoDaVarredura:
    publicados: set[str]
    falhos: set[str]
    varridos: int  # itens antigos lidos nesta rodada
    interrompida: bool  # parou no teto ou no orçamento: pode haver mais, a próxima rodada continua


def varrer(
    outbox: Outbox,
    publicador: Publicador,
    agora_ms: int,
    idade_minima_ms: int = IDADE_MINIMA_MS,
    *,
    teto_de_itens: int = TETO_DE_ITENS,
    prazo_s: float = ORCAMENTO_S,
    relogio: Callable[[], float] = time.monotonic,
) -> ResultadoDaVarredura:
    """Publica página a página. O teto e o prazo são conferidos entre páginas (a página já lida sai inteira)."""
    fim = relogio() + prazo_s
    publicados: set[str] = set()
    falhos: set[str] = set()
    varridos = 0
    for pagina in outbox.paginas_antigas(agora_ms - idade_minima_ms):
        resultado = processar(pagina, publicador, outbox)
        publicados |= resultado.publicados
        falhos |= resultado.falhos
        varridos += len(pagina)
        if varridos >= teto_de_itens or relogio() >= fim:
            return ResultadoDaVarredura(publicados, falhos, varridos, interrompida=True)
    return ResultadoDaVarredura(publicados, falhos, varridos, interrompida=False)


def _prazo(contexto: object) -> float:
    """Orçamento da rodada: o fixo, ou menos se a Lambda tiver menos tempo restante (timeout mudou na IaC)."""
    restante_ms = getattr(contexto, "get_remaining_time_in_millis", None)
    if restante_ms is None:
        return ORCAMENTO_S
    return max(0.0, min(ORCAMENTO_S, float(restante_ms()) / 1000 - MARGEM_S))


def handler_agendado(evento: dict[str, Any], contexto: object) -> dict[str, int | bool]:
    """Alvo do EventBridge Scheduler (AWS)."""
    c = componentes()
    resultado = varrer(c.outbox, c.publicador, int(time.time() * 1000), prazo_s=_prazo(contexto))
    resumo: dict[str, int | bool] = {
        "publicados": len(resultado.publicados),
        "falhos": len(resultado.falhos),
        "varridos": resultado.varridos,
        "interrompida": resultado.interrompida,
    }
    log.info(json.dumps({"msg": "sweeper", **resumo}))
    return resumo
