"""Relay ponta a ponta na LocalStack: outbox -> Stream -> poller -> EventBridge -> SQS.

A tabela e o item são os do produtor REAL (`definicoes_das_tabelas` e `_item_do_outbox` do BC Autorização), não uma
cópia: se o layout do item mudar de um lado só, o teste fica vermelho (C7-6, painel 7).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from autorizacao.adapters.outbound.dynamodb import _item_do_outbox, definicoes_das_tabelas
from autorizacao.domain.eventos import SaldosDoLimite, TransacaoAutorizada
from relay.eventbridge import PublicadorEventBridge
from relay.local import LeitorDeStream, cliente_local, uma_iteracao
from relay.outbox_dynamodb import OutboxDynamoDB
from relay.sweeper import varrer

from relay import lambda_handler

pytestmark = pytest.mark.integracao

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")


def evento(ocorrido_em: datetime | None = None) -> TransacaoAutorizada:
    return TransacaoAutorizada(
        id_evento=str(uuid.uuid4()),
        ocorrido_em=ocorrido_em or datetime.now(UTC),
        id_contrato="CTR-000123456",
        correlation_id=str(uuid.uuid4()),
        traceparent=None,
        id_autorizacao=str(uuid.uuid4()),
        tipo_operacao="DEBITO",
        valor=Decimal("150.00"),
        moeda="BRL",
        id_conta="0001-00000028-9",
        id_estabelecimento=None,
        saldos=SaldosDoLimite(
            Decimal("1000.00"), Decimal("150.00"), Decimal("0.00"), Decimal("850.00"), 2
        ),
    )


def sem_detail_type(e: TransacaoAutorizada) -> dict[str, Any]:
    """O item real menos um campo que o relay publica: o 'skew de layout' de um deploy."""
    return {k: v for k, v in _item_do_outbox(e).items() if k != "detail_type"}


@pytest.fixture
def ambiente() -> Iterator[dict[str, Any]]:
    dynamodb = cliente_local("dynamodb", ENDPOINT)
    streams = cliente_local("dynamodbstreams", ENDPOINT)
    events = cliente_local("events", ENDPOINT)
    sqs = cliente_local("sqs", ENDPOINT)
    sufixo = uuid.uuid4().hex[:8]
    tabela, bus, fila = f"outbox-teste-{sufixo}", f"bus-teste-{sufixo}", f"fila-teste-{sufixo}"

    (definicao_do_outbox,) = [
        d for d in definicoes_das_tabelas(f"aut-teste-{sufixo}", tabela) if d["TableName"] == tabela
    ]
    dynamodb.create_table(**definicao_do_outbox)
    dynamodb.get_waiter("table_exists").wait(TableName=tabela)
    arn_stream = dynamodb.describe_table(TableName=tabela)["Table"]["LatestStreamArn"]

    for _ in range(40):  # gravar antes de ENABLED perde o registro na LocalStack
        status = streams.describe_stream(StreamArn=arn_stream)["StreamDescription"]["StreamStatus"]
        if status == "ENABLED":
            break
        time.sleep(0.25)
    assert status == "ENABLED"

    events.create_event_bus(Name=bus)
    url = sqs.create_queue(QueueName=fila)["QueueUrl"]
    arn_fila = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    events.put_rule(
        Name="r",
        EventBusName=bus,
        EventPattern=json.dumps(
            {"source": ["financas.autorizacao"], "detail-type": ["TransacaoAutorizada"]}
        ),
    )
    events.put_targets(Rule="r", EventBusName=bus, Targets=[{"Id": "t", "Arn": arn_fila}])
    try:
        yield {
            "dynamodb": dynamodb,
            "streams": streams,
            "arn_stream": arn_stream,
            "sqs": sqs,
            "url": url,
            "tabela": tabela,
            "bus": bus,
            "events": events,
            "leitor": LeitorDeStream(streams, arn_stream),
        }
    finally:
        events.remove_targets(Rule="r", EventBusName=bus, Ids=["t"])
        events.delete_rule(Name="r", EventBusName=bus)
        events.delete_event_bus(Name=bus)
        sqs.delete_queue(QueueUrl=url)
        dynamodb.delete_table(TableName=tabela)


def _gravar(ambiente: dict[str, Any], item: dict[str, Any]) -> None:
    ambiente["dynamodb"].put_item(TableName=ambiente["tabela"], Item=item)


def _ids_na_fila(ambiente: dict[str, Any], esperados: int) -> list[str]:
    ids: list[str] = []
    for _ in range(5):
        mensagens = (
            ambiente["sqs"]
            .receive_message(QueueUrl=ambiente["url"], MaxNumberOfMessages=10, WaitTimeSeconds=2)
            .get("Messages", [])
        )
        ids += [json.loads(m["Body"])["detail"]["id"] for m in mensagens]
        if len(ids) >= esperados:
            break
    return ids


def _no_outbox(ambiente: dict[str, Any]) -> set[str]:
    return {i["pk"]["S"] for i in ambiente["dynamodb"].scan(TableName=ambiente["tabela"])["Items"]}


def test_outbox_chega_na_fila_e_sai_do_outbox(ambiente: dict[str, Any]) -> None:
    e = evento()
    _gravar(ambiente, _item_do_outbox(e))
    publicador = PublicadorEventBridge(ambiente["events"], ambiente["bus"])
    outbox = OutboxDynamoDB(ambiente["dynamodb"], ambiente["tabela"])

    publicados: set[str] = set()
    for _ in range(20):  # o Stream da LocalStack pode demorar a expor o registro
        publicados |= uma_iteracao(ambiente["leitor"], publicador, outbox).publicados
        if publicados:
            break
        time.sleep(0.5)
    assert publicados == {e.id_evento}

    mensagens = (
        ambiente["sqs"]
        .receive_message(QueueUrl=ambiente["url"], MaxNumberOfMessages=10, WaitTimeSeconds=5)
        .get("Messages", [])
    )
    assert len(mensagens) == 1
    envelope = json.loads(mensagens[0]["Body"])
    assert envelope["detail-type"] == "TransacaoAutorizada"
    assert envelope["source"] == "financas.autorizacao"
    assert envelope["detail"] == json.loads(_item_do_outbox(e)["cloudevent"]["S"])
    assert _no_outbox(ambiente) == set()


def test_handler_lambda_devolve_o_malformado_do_stream_real(
    ambiente: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Registros que o Stream da LocalStack entrega, no handler da Lambda: o malformado volta como falha do lote."""
    bom, ruim = evento(), evento()
    _gravar(ambiente, _item_do_outbox(bom))
    _gravar(ambiente, sem_detail_type(ruim))
    streams, arn = ambiente["streams"], ambiente["arn_stream"]
    shard = streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"][0]["ShardId"]
    iterador = streams.get_shard_iterator(
        StreamArn=arn, ShardId=shard, ShardIteratorType="TRIM_HORIZON"
    )["ShardIterator"]
    registros: list[dict[str, Any]] = []
    for _ in range(20):
        resposta = streams.get_records(ShardIterator=iterador)
        registros += resposta["Records"]
        iterador = resposta["NextShardIterator"]
        if len(registros) >= 2:
            break
        time.sleep(0.5)
    sequencia_do_ruim = next(
        r["dynamodb"]["SequenceNumber"]
        for r in registros
        if r["dynamodb"]["NewImage"]["id_evento"]["S"] == ruim.id_evento
    )
    monkeypatch.setattr(
        lambda_handler,
        "_componentes",
        lambda_handler.Componentes(
            PublicadorEventBridge(ambiente["events"], ambiente["bus"]),
            OutboxDynamoDB(ambiente["dynamodb"], ambiente["tabela"]),
        ),
    )

    with caplog.at_level(logging.ERROR, logger="relay.lambda_handler"):
        resposta = lambda_handler.handler({"Records": registros}, None)

    assert resposta == {"batchItemFailures": [{"itemIdentifier": sequencia_do_ruim}]}
    assert _ids_na_fila(ambiente, 1) == [bom.id_evento]
    assert _no_outbox(ambiente) == {f"OUTBOX#{ruim.id_evento}"}  # fica para corrigir; não some
    linha = json.loads(caplog.records[-1].getMessage())
    assert (linha["evento"], linha["pk"]) == (
        "outbox_registro_malformado",
        f"OUTBOX#{ruim.id_evento}",
    )


def test_sweeper_republica_so_o_antigo_e_loga_o_malformado(
    ambiente: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Scan com Limit + ProjectionExpression no DynamoDB da LocalStack: o antigo sai e é apagado; o antigo
    malformado fica na tabela e é logado com o pk a cada rodada."""
    agora = datetime.now(UTC)
    antigo, novo, ruim = (
        evento(agora - timedelta(minutes=20)),
        evento(agora),
        evento(agora - timedelta(minutes=20)),
    )
    _gravar(ambiente, _item_do_outbox(antigo))
    _gravar(ambiente, _item_do_outbox(novo))
    _gravar(ambiente, sem_detail_type(ruim))
    publicador = PublicadorEventBridge(ambiente["events"], ambiente["bus"])
    outbox = OutboxDynamoDB(ambiente["dynamodb"], ambiente["tabela"])

    with caplog.at_level(logging.ERROR, logger="relay.outbox_dynamodb"):
        resultado = varrer(
            outbox, publicador, int(agora.timestamp() * 1000)
        )  # idade mínima: 15 min

    assert (resultado.publicados, resultado.falhos, resultado.interrompida) == (
        {antigo.id_evento},
        set(),
        False,
    )
    assert _no_outbox(ambiente) == {f"OUTBOX#{novo.id_evento}", f"OUTBOX#{ruim.id_evento}"}
    assert _ids_na_fila(ambiente, 1) == [antigo.id_evento]
    linhas = [json.loads(r.getMessage()) for r in caplog.records]
    assert [(x["evento"], x["pk"]) for x in linhas] == [
        ("outbox_registro_malformado", f"OUTBOX#{ruim.id_evento}")
    ]
