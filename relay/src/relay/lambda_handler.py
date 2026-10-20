"""Entrada AWS: Lambda com event source mapping de DynamoDB Streams (ReportBatchItemFailures)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3

from relay.cdc_limites import SOURCE_NO_BUS, OutboxNoop, de_registro_do_stream_de_limites
from relay.eventbridge import PublicadorEventBridge
from relay.nucleo import EventoPendente, processar
from relay.outbox_dynamodb import OutboxDynamoDB, de_registro_do_stream

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


@dataclass(frozen=True)
class Componentes:
    publicador: PublicadorEventBridge
    outbox: OutboxDynamoDB


_componentes: Componentes | None = None


def componentes() -> Componentes:
    """Clientes boto3 criados uma vez por container (lazy, para importar sem ambiente)."""
    global _componentes  # noqa: PLW0603
    if _componentes is None:
        regiao = os.environ.get("AWS_REGION")
        _componentes = Componentes(
            publicador=PublicadorEventBridge(
                boto3.client("events", region_name=regiao), os.environ["BUS"]
            ),
            outbox=OutboxDynamoDB(
                boto3.client("dynamodb", region_name=regiao), os.environ["TABELA_OUTBOX"]
            ),
        )
    return _componentes


def handler(evento: dict[str, Any], contexto: object) -> dict[str, list[dict[str, str]]]:
    c = componentes()
    pendentes: list[EventoPendente] = []
    sequencia_de: dict[str, str] = {}
    for registro in evento.get("Records", []):
        pendente = de_registro_do_stream(registro)
        if pendente is not None:
            pendentes.append(pendente)
            sequencia_de[pendente.id_evento] = registro["dynamodb"]["SequenceNumber"]
    resultado = processar(pendentes, c.publicador, c.outbox)
    log.info(
        '{"msg":"lote","publicados":%d,"falhos":%d}',
        len(resultado.publicados),
        len(resultado.falhos),
    )
    return {
        "batchItemFailures": [{"itemIdentifier": sequencia_de[i]} for i in sorted(resultado.falhos)]
    }


@dataclass(frozen=True)
class ComponentesCdcLimites:
    publicador: PublicadorEventBridge
    outbox: OutboxNoop


_cdc_limites: ComponentesCdcLimites | None = None


def componentes_cdc_limites() -> ComponentesCdcLimites:
    """Função separada do relay do outbox: lê só BUS (não há tabela de outbox nesta fonte)."""
    global _cdc_limites  # noqa: PLW0603
    if _cdc_limites is None:
        _cdc_limites = ComponentesCdcLimites(
            publicador=PublicadorEventBridge(
                boto3.client("events", region_name=os.environ.get("AWS_REGION")),
                os.environ["BUS"],
                source=SOURCE_NO_BUS,
            ),
            outbox=OutboxNoop(),
        )
    return _cdc_limites


def handler_cdc_limites(
    evento: dict[str, Any], contexto: object
) -> dict[str, list[dict[str, str]]]:
    """Stream da tabela `limites` → LimiteAlterado no bus. Falhou = só o item volta (batchItemFailures)."""
    c = componentes_cdc_limites()
    pendentes: list[EventoPendente] = []
    sequencia_de: dict[str, str] = {}
    for registro in evento.get("Records", []):
        pendente = de_registro_do_stream_de_limites(registro)
        if pendente is not None:
            pendentes.append(pendente)
            sequencia_de[pendente.id_evento] = registro["dynamodb"]["SequenceNumber"]
    resultado = processar(pendentes, c.publicador, c.outbox)
    log.info(
        '{"msg":"lote_cdc_limites","publicados":%d,"falhos":%d}',
        len(resultado.publicados),
        len(resultado.falhos),
    )
    return {
        "batchItemFailures": [{"itemIdentifier": sequencia_de[i]} for i in sorted(resultado.falhos)]
    }
