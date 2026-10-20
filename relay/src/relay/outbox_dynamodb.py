"""Outbox sobre DynamoDB e conversão de registros do Streams."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.types import TypeDeserializer

from relay.nucleo import EventoPendente

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

log = logging.getLogger(__name__)

PREFIXO = "OUTBOX#"
LOTE_DE_DELETE = 25  # limite do BatchWriteItem
TENTATIVAS_DE_NAO_PROCESSADOS = 3
_deserializador = TypeDeserializer()


class OutboxDynamoDB:
    def __init__(self, cliente: DynamoDBClient, tabela: str) -> None:
        self._cliente = cliente
        self._tabela = tabela

    def apagar(self, ids: Iterable[str]) -> None:
        unicos = list(dict.fromkeys(ids))  # BatchWriteItem rejeita chave repetida no lote
        for inicio in range(0, len(unicos), LOTE_DE_DELETE):
            pedidos: list[Any] = [
                {"DeleteRequest": {"Key": {"pk": {"S": f"{PREFIXO}{i}"}}}}
                for i in unicos[inicio : inicio + LOTE_DE_DELETE]
            ]
            for _ in range(TENTATIVAS_DE_NAO_PROCESSADOS):
                resposta = self._cliente.batch_write_item(RequestItems={self._tabela: pedidos})
                pedidos = list(resposta.get("UnprocessedItems", {}).get(self._tabela, []))
                if not pedidos:
                    break
            if pedidos:
                # o evento já saiu; o item que sobrar será republicado pelo sweeper (at-least-once)
                log.warning("%d deletes não processados em %s", len(pedidos), self._tabela)

    def listar_antigos(self, antes_de_ms: int) -> list[EventoPendente]:
        pendentes: list[EventoPendente] = []
        argumentos: dict[str, Any] = {
            "TableName": self._tabela,
            "FilterExpression": "criado_em_ms < :t",
            "ExpressionAttributeValues": {":t": {"N": str(antes_de_ms)}},
        }
        while True:
            pagina = self._cliente.scan(**argumentos)
            for item in pagina.get("Items", []):
                pendente = _de_imagem(item)
                if pendente is not None:
                    pendentes.append(pendente)
            chave = pagina.get("LastEvaluatedKey")
            if not chave:
                return pendentes
            argumentos["ExclusiveStartKey"] = chave


def _de_imagem(imagem: Mapping[str, Any]) -> EventoPendente | None:
    try:
        item = {k: _deserializador.deserialize(v) for k, v in imagem.items()}
        if not str(item["pk"]).startswith(PREFIXO):
            return None
        return EventoPendente(
            id_evento=str(item["id_evento"]),
            detail_type=str(item["detail_type"]),
            cloudevent=str(item["cloudevent"]),
            criado_em_ms=int(item["criado_em_ms"]),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("item de outbox malformado ignorado")
        return None


def de_registro_do_stream(registro: Mapping[str, Any]) -> EventoPendente | None:
    """Registro do Streams (evento Lambda ou GetRecords) -> pendente. Só INSERT; o resto é None."""
    if registro.get("eventName") != "INSERT":
        return None
    imagem = registro.get("dynamodb", {}).get("NewImage")
    if not imagem:
        return None
    return _de_imagem(imagem)
