"""Entrada AWS: Lambda com event source mapping de DynamoDB Streams (ReportBatchItemFailures)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from relay.cdc_limites import (
    SOURCE_NO_BUS,
    OutboxNoop,
    RegistroMalformado,
    de_registro_do_stream_de_limites,
)
from relay.eventbridge import PublicadorEventBridge
from relay.nucleo import EventoPendente, processar
from relay.outbox_dynamodb import ItemDeOutboxMalformado, OutboxDynamoDB, de_registro_do_stream

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# Endpoint só o da AWS: uma `AWS_ENDPOINT_URL[_<SERVIÇO>]` na configuração da função (ou um `~/.aws/config`) não
# desvia o relay, o sweeper nem o CDC. Mesma trava dos composition roots dos serviços.
SO_ENDPOINT_DA_AWS = Config(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
    ignore_configured_endpoint_urls=True,
)


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
                boto3.client("events", region_name=regiao, config=SO_ENDPOINT_DA_AWS),
                os.environ["BUS"],
            ),
            outbox=OutboxDynamoDB(
                boto3.client("dynamodb", region_name=regiao, config=SO_ENDPOINT_DA_AWS),
                os.environ["TABELA_OUTBOX"],
            ),
        )
    return _componentes


def handler(evento: dict[str, Any], contexto: object) -> dict[str, list[dict[str, str]]]:
    """Stream do outbox → bus. Falhou = só o item volta (batchItemFailures).

    Item malformado também volta como falha, logado com o `pk`: o ESM tenta de novo e, esgotadas as tentativas,
    manda o lote para a DLQ on-failure (com alarme). O item fica no outbox. Nunca é descartado em silêncio.
    """
    c = componentes()
    pendentes: list[EventoPendente] = []
    sequencia_de: dict[str, str] = {}
    malformados: list[str] = []
    for registro in evento.get("Records", []):
        sequencia = registro["dynamodb"]["SequenceNumber"]
        try:
            pendente = de_registro_do_stream(registro)
        except ItemDeOutboxMalformado as erro:
            log.error(
                json.dumps(
                    {
                        "evento": "outbox_registro_malformado",
                        "pk": erro.pk,
                        "sequencia": sequencia,
                        "erro": str(erro),
                    }
                )
            )
            malformados.append(sequencia)
            continue
        if pendente is not None:
            pendentes.append(pendente)
            sequencia_de[pendente.id_evento] = sequencia
    resultado = processar(pendentes, c.publicador, c.outbox)
    log.info(
        '{"msg":"lote","publicados":%d,"falhos":%d,"malformados":%d}',
        len(resultado.publicados),
        len(resultado.falhos),
        len(malformados),
    )
    falhas = [sequencia_de[i] for i in sorted(resultado.falhos)] + malformados
    return {"batchItemFailures": [{"itemIdentifier": s} for s in falhas]}


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
                boto3.client(
                    "events", region_name=os.environ.get("AWS_REGION"), config=SO_ENDPOINT_DA_AWS
                ),
                os.environ["BUS"],
                source=SOURCE_NO_BUS,
            ),
            outbox=OutboxNoop(),
        )
    return _cdc_limites


def handler_cdc_limites(
    evento: dict[str, Any], contexto: object
) -> dict[str, list[dict[str, str]]]:
    """Stream da tabela `limites` → LimiteAlterado no bus. Falhou = só o item volta (batchItemFailures).

    Registro malformado também volta como falha: o ESM tenta de novo e, esgotadas as tentativas, manda o
    lote para a DLQ on-failure (com alarme). Nunca é descartado em silêncio.
    """
    c = componentes_cdc_limites()
    pendentes: list[EventoPendente] = []
    sequencia_de: dict[str, str] = {}
    malformados: list[str] = []
    for registro in evento.get("Records", []):
        sequencia = registro["dynamodb"]["SequenceNumber"]
        try:
            pendente = de_registro_do_stream_de_limites(registro)
        except RegistroMalformado as erro:
            log.error(
                json.dumps(
                    {"evento": "cdc_registro_malformado", "sequencia": sequencia, "erro": str(erro)}
                )
            )
            malformados.append(sequencia)
            continue
        if pendente is not None:
            pendentes.append(pendente)
            sequencia_de[pendente.id_evento] = sequencia
    resultado = processar(pendentes, c.publicador, c.outbox)
    log.info(
        '{"msg":"lote_cdc_limites","publicados":%d,"falhos":%d,"malformados":%d}',
        len(resultado.publicados),
        len(resultado.falhos),
        len(malformados),
    )
    falhas = [sequencia_de[i] for i in sorted(resultado.falhos)] + malformados
    return {"batchItemFailures": [{"itemIdentifier": s} for s in falhas]}
