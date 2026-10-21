"""CDC da tabela `limites`: registro do Stream (MODIFY) → LimiteAlterado (CloudEvent).

Só vira evento quando o LEGADO mexeu no que importa para os consumidores: `concedido` ou `status`.
Reserva, débito e a simples troca de `versao_legado` (SEM_MUDANCA) não geram nada (volumetria baixa,
ver contracts/events/limite-alterado.v1.schema.json).

Idempotência: o `id` do CloudEvent é UUIDv5 de `id_contrato|versao` (a `versao` do item sobe 1 a cada
escrita no contrato: reserva, débito, crédito, estorno, expiração E atualização do legado; é monotônica
por item), então reentregas do Stream produzem o MESMO evento e o consumidor deduplica por (source, id).
Para esta fonte não há outbox a apagar: `OutboxNoop` satisfaz o núcleo do relay.

Registro malformado (MODIFY sem as imagens ou sem os campos do contrato) NÃO é descartado: levanta
`RegistroMalformado`, e a entrada decide. Na Lambda o registro volta como falha do lote (retry do ESM e,
esgotadas as tentativas, a DLQ on-failure com alarme); descartar perderia um LimiteAlterado sem sinal.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import simplejson
from boto3.dynamodb.types import TypeDeserializer

from relay.nucleo import EventoPendente

SOURCE_DO_EVENTO = "/financas/limites"  # atributo CE `source` (contrato)
SOURCE_NO_BUS = "financas.limites"  # `Source` do PutEvents (roteamento por regra)
TIPO = "example.financas.limites.LimiteAlterado.v1"
NOME_CURTO = "LimiteAlterado"
DATASCHEMA = "https://financas.example/schemas/eventos/limite-alterado/1.0.json"
VERSAO_DO_SCHEMA = "1.0"
NAMESPACE = uuid.UUID("5c1b7a52-8e0d-4f6b-9a3e-2d4c6b8a0f13")
_deserializador = TypeDeserializer()


class RegistroMalformado(ValueError):
    """MODIFY da tabela `limites` que não dá para converter: vai para retry/DLQ, nunca some em silêncio."""


class OutboxNoop:
    """Esta fonte não tem outbox: o Stream da tabela é a fila durável (retry pelo próprio Stream)."""

    def apagar(self, ids: Iterable[str]) -> None:
        return None

    def paginas_antigas(self, antes_de_ms: int) -> Iterator[list[EventoPendente]]:
        return iter(())


def _instante(registro: Mapping[str, Any]) -> datetime:
    """Instante do fato: ApproximateCreationDateTime (epoch no Lambda, datetime no GetRecords)."""
    bruto = registro.get("dynamodb", {}).get("ApproximateCreationDateTime")
    if isinstance(bruto, datetime):
        return bruto.astimezone(UTC)
    if isinstance(bruto, int | float | Decimal):
        return datetime.fromtimestamp(float(bruto), UTC)
    return datetime.now(UTC)


def _imagem(imagem: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _deserializador.deserialize(v) for k, v in imagem.items()}


def de_registro_do_stream_de_limites(registro: Mapping[str, Any]) -> EventoPendente | None:
    """MODIFY com OldImage e NewImage → LimiteAlterado, só se concedido/status mudaram; senão None.

    Levanta `RegistroMalformado` se o MODIFY não traz as duas imagens (Stream sem NEW_AND_OLD_IMAGES) ou
    os campos do contrato.
    """
    if registro.get("eventName") != "MODIFY":
        return None
    corpo = registro.get("dynamodb", {})
    if not corpo.get("OldImage") or not corpo.get("NewImage"):
        raise RegistroMalformado("MODIFY sem OldImage/NewImage")
    try:
        antes, depois = _imagem(corpo["OldImage"]), _imagem(corpo["NewImage"])
        if antes["concedido"] == depois["concedido"] and antes["status"] == depois["status"]:
            return None
        id_contrato = str(depois["id_contrato"])
        versao = int(depois["versao"])
        instante = _instante(registro)
        marca = instante.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        id_evento = str(uuid.uuid5(NAMESPACE, f"{id_contrato}|{versao}"))
        correlacao = str(uuid.uuid5(NAMESPACE, f"correlacao|{id_contrato}|{versao}"))
        cloudevent = {
            "specversion": "1.0",
            "id": id_evento,
            "source": SOURCE_DO_EVENTO,
            "type": TIPO,
            "time": marca,
            "subject": id_contrato,
            "datacontenttype": "application/json",
            "dataschema": DATASCHEMA,
            "correlationid": correlacao,
            "partitionkey": id_contrato,
            "data": {
                "event_id": id_evento,
                "event_type": NOME_CURTO,
                "event_version": VERSAO_DO_SCHEMA,
                "occurred_at": marca,
                "id_contrato": id_contrato,
                "correlation_id": correlacao,
                "trace_id": None,
                "limite_concedido_anterior": Decimal(antes["concedido"]),
                "limite_concedido": Decimal(depois["concedido"]),
                "status_contrato_anterior": str(antes["status"]),
                "status_contrato": str(depois["status"]),
                "limite_disponivel": Decimal(depois["disponivel"]),
                "versao_limite": versao,
                "versao_legado": int(depois["versao_legado"]),
                "origem": "CORE_LEGADO",
            },
        }
    except (KeyError, TypeError, ValueError, ArithmeticError) as erro:
        raise RegistroMalformado(
            f"campo ausente ou inválido ({type(erro).__name__}: {erro})"
        ) from erro
    return EventoPendente(
        id_evento=id_evento,
        detail_type=NOME_CURTO,
        # número exato (Decimal), nunca float
        cloudevent=str(
            simplejson.dumps(
                cloudevent, use_decimal=True, ensure_ascii=False, separators=(",", ":")
            )
        ),
        criado_em_ms=int(instante.timestamp() * 1000),
    )
