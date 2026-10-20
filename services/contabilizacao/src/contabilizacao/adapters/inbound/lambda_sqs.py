"""Entrada AWS: Lambda com event source mapping de SQS (ReportBatchItemFailures)."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from contabilizacao.adapters.inbound.eventbridge_sqs import cloudevent_da_mensagem
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.config import do_ambiente
from contabilizacao.main import construir_caso_de_uso

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_caso_de_uso: ContabilizarEvento | None = None


def caso_de_uso() -> ContabilizarEvento:
    """Clientes boto3 criados uma vez por container (lazy, para importar sem ambiente).
    Lê TABELA_LANCAMENTOS, TABELA_INBOX e AWS_REGION."""
    global _caso_de_uso  # noqa: PLW0603
    if _caso_de_uso is None:
        _caso_de_uso = construir_caso_de_uso(do_ambiente())
    return _caso_de_uso


def handler(evento: dict[str, Any], contexto: object) -> dict[str, list[dict[str, str]]]:
    falhas: list[dict[str, str]] = []
    desfechos: Counter[str] = Counter()
    for registro in evento.get("Records", []):
        try:
            cloudevent = cloudevent_da_mensagem(registro["body"])
            desfechos[caso_de_uso().executar(cloudevent).value] += 1
        except Exception:
            # QUALQUER falha (evento inválido inclusive) devolve só ESTE item: o SQS o reentrega e,
            # após maxReceiveCount tentativas, move para a DLQ. Os demais do lote não são reprocessados.
            log.exception("falha ao contabilizar a mensagem %s", registro.get("messageId"))
            desfechos["FALHA"] += 1
            falhas.append({"itemIdentifier": registro["messageId"]})
    log.info(json.dumps({"msg": "lote", **desfechos}))
    return {"batchItemFailures": falhas}
