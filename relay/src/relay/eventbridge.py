"""Publicador no EventBridge via PutEvents."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from relay.nucleo import EventoPendente

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient

log = logging.getLogger(__name__)

SOURCE_PADRAO = "financas.autorizacao"


class PublicadorEventBridge:
    def __init__(self, cliente: EventBridgeClient, bus: str, source: str = SOURCE_PADRAO) -> None:
        self._cliente = cliente
        self._bus = bus
        self._source = source

    def publicar(self, lote: Sequence[EventoPendente]) -> set[str]:
        if not lote:
            return set()
        entradas: list[Any] = [
            {
                "Source": self._source,
                "DetailType": e.detail_type,
                # a string original, sem json.loads/dumps: preserva o número exato
                "Detail": e.cloudevent,
                "EventBusName": self._bus,
            }
            for e in lote
        ]
        try:
            resposta = self._cliente.put_events(Entries=entradas)
        except (ClientError, BotoCoreError):
            log.exception("put_events falhou; nenhum evento do lote publicado")
            return set()
        resultados = resposta.get("Entries", [])
        if len(resultados) != len(lote):  # resposta inesperada: não presumir sucesso
            log.error("put_events devolveu %d entradas para %d", len(resultados), len(lote))
            return set()
        publicados: set[str] = set()
        for evento, resultado in zip(lote, resultados, strict=True):
            if "ErrorCode" in resultado:
                log.warning(
                    "evento %s rejeitado: %s %s",
                    evento.id_evento,
                    resultado.get("ErrorCode"),
                    resultado.get("ErrorMessage"),
                )
            else:
                publicados.add(evento.id_evento)
        return publicados
