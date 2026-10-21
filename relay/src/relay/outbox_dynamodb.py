"""Outbox sobre DynamoDB e conversão de registros do Streams.

Item do outbox malformado (sem os campos que o relay publica) NÃO é descartado: levanta
`ItemDeOutboxMalformado`, e a entrada decide. Na Lambda o registro volta como falha do lote (retry do ESM e,
esgotadas as tentativas, a DLQ on-failure com alarme); descartar perderia um evento de domínio sem sinal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.types import TypeDeserializer

from relay.nucleo import EventoPendente

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

log = logging.getLogger(__name__)

PREFIXO = "OUTBOX#"
LOTE_DE_DELETE = 25  # limite do BatchWriteItem
TENTATIVAS_DE_NAO_PROCESSADOS = 3
# Limit do Scan: itens AVALIADOS por página (antes do filtro); limita a memória do sweeper.
ITENS_POR_PAGINA = 500
_deserializador = TypeDeserializer()


class ItemDeOutboxMalformado(ValueError):
    """Item `OUTBOX#` sem os campos que o relay publica: vai para retry/DLQ, nunca some em silêncio.

    `pk` é a chave do item, quando o registro a traz: é o que o operador procura na tabela.
    """

    def __init__(self, pk: str | None, motivo: str) -> None:
        super().__init__(motivo)
        self.pk = pk


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

    def paginas_antigas(
        self, antes_de_ms: int, itens_por_pagina: int = ITENS_POR_PAGINA
    ) -> Iterator[list[EventoPendente]]:
        """Scan com `Limit`, uma página por vez: nada se acumula aqui, e quem consome publica a página e
        decide se pede a próxima. Página vazia (o filtro descartou tudo) também sai, para o consumidor
        conferir o orçamento de tempo mesmo numa tabela grande sem item antigo.

        Item malformado que o filtro devolve (o `criado_em_ms` numérico e antigo) não entra na página: é
        logado com o `pk` (`outbox_registro_malformado`, com metric filter e alarme) e fica na tabela, e toda
        rodada seguinte o loga de novo. Sem `criado_em_ms` válido o filtro não o devolve; esse caso o relay
        já pegou na entrada do Stream (falha do lote, DLQ e o mesmo termo de log)."""
        argumentos: dict[str, Any] = {
            "TableName": self._tabela,
            "Limit": itens_por_pagina,
            "ProjectionExpression": "pk, id_evento, detail_type, cloudevent, criado_em_ms",
            "FilterExpression": "criado_em_ms < :t",
            "ExpressionAttributeValues": {":t": {"N": str(antes_de_ms)}},
        }
        while True:
            pagina = self._cliente.scan(**argumentos)
            yield _pendentes_da_pagina(pagina.get("Items", []))
            chave = pagina.get("LastEvaluatedKey")
            if not chave:
                return
            argumentos["ExclusiveStartKey"] = chave


def _pk(imagem: Mapping[str, Any] | None) -> str | None:
    pk = (imagem or {}).get("pk")
    return pk.get("S") if isinstance(pk, Mapping) and isinstance(pk.get("S"), str) else None


def _de_imagem(imagem: Mapping[str, Any]) -> EventoPendente | None:
    """Imagem do item -> pendente. None só para item que não é do outbox (pk sem `OUTBOX#`); item sem `pk`
    ou do outbox sem os campos que o relay publica levanta `ItemDeOutboxMalformado`."""
    pk = _pk(imagem)
    if pk is None:
        raise ItemDeOutboxMalformado(None, "item sem pk")
    if not pk.startswith(PREFIXO):
        return None
    try:
        item = {k: _deserializador.deserialize(v) for k, v in imagem.items()}
        return EventoPendente(
            id_evento=str(item["id_evento"]),
            detail_type=str(item["detail_type"]),
            cloudevent=str(item["cloudevent"]),
            criado_em_ms=int(item["criado_em_ms"]),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as erro:
        raise ItemDeOutboxMalformado(
            pk, f"campo ausente ou inválido ({type(erro).__name__}: {erro})"
        ) from erro


def _pendentes_da_pagina(itens: Iterable[Mapping[str, Any]]) -> list[EventoPendente]:
    pendentes: list[EventoPendente] = []
    for item in itens:
        try:
            pendente = _de_imagem(item)
        except ItemDeOutboxMalformado as erro:
            log.error(
                json.dumps(
                    {"evento": "outbox_registro_malformado", "pk": erro.pk, "erro": str(erro)}
                )
            )
            continue
        if pendente is not None:
            pendentes.append(pendente)
    return pendentes


def de_registro_do_stream(registro: Mapping[str, Any]) -> EventoPendente | None:
    """Registro do Streams (evento Lambda ou GetRecords) -> pendente. Só INSERT; o resto é None.

    INSERT sem `NewImage` (Stream sem NEW_IMAGE) ou com item do outbox malformado levanta
    `ItemDeOutboxMalformado`: a entrada decide (a Lambda devolve como falha do lote).
    """
    if registro.get("eventName") != "INSERT":
        return None
    corpo = registro.get("dynamodb", {})
    imagem = corpo.get("NewImage")
    if not imagem:
        raise ItemDeOutboxMalformado(_pk(corpo.get("Keys")), "INSERT sem NewImage")
    return _de_imagem(imagem)
