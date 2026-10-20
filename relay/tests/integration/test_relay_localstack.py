"""Relay ponta a ponta na LocalStack: outbox -> Stream -> poller -> EventBridge -> SQS."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from relay.eventbridge import PublicadorEventBridge
from relay.local import LeitorDeStream, cliente_local, uma_iteracao
from relay.outbox_dynamodb import OutboxDynamoDB

pytestmark = pytest.mark.integracao

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")
EXEMPLO = next(
    p / "contracts/events/examples/transacao-autorizada.v1.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/events/examples/transacao-autorizada.v1.json").exists()
)


@pytest.fixture
def ambiente() -> Iterator[dict[str, Any]]:
    dynamodb = cliente_local("dynamodb", ENDPOINT)
    streams = cliente_local("dynamodbstreams", ENDPOINT)
    events = cliente_local("events", ENDPOINT)
    sqs = cliente_local("sqs", ENDPOINT)
    sufixo = uuid.uuid4().hex[:8]
    tabela, bus, fila = f"outbox-teste-{sufixo}", f"bus-teste-{sufixo}", f"fila-teste-{sufixo}"

    # mesma definição de autorizacao.adapters.outbound.dynamodb.definicoes_das_tabelas (outbox)
    dynamodb.create_table(
        TableName=tabela,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
    )
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


def test_outbox_chega_na_fila_e_sai_do_outbox(ambiente: dict[str, Any]) -> None:
    cloudevent = EXEMPLO.read_text()
    id_evento = json.loads(cloudevent)["id"]
    ddb = ambiente["dynamodb"]
    ddb.put_item(
        TableName=ambiente["tabela"],
        Item={
            "pk": {"S": f"OUTBOX#{id_evento}"},
            "id_evento": {"S": id_evento},
            "detail_type": {"S": "TransacaoAutorizada"},
            "cloudevent": {"S": cloudevent},
            "id_contrato": {"S": "CTR-000123456"},
            "criado_em_ms": {"N": str(int(time.time() * 1000))},
            "ttl": {"N": str(int(time.time()) + 3600)},
        },
    )
    publicador = PublicadorEventBridge(ambiente["events"], ambiente["bus"])
    outbox = OutboxDynamoDB(ddb, ambiente["tabela"])

    publicados: set[str] = set()
    for _ in range(20):  # o Stream da LocalStack pode demorar a expor o registro
        publicados |= uma_iteracao(ambiente["leitor"], publicador, outbox).publicados
        if publicados:
            break
        time.sleep(0.5)
    assert publicados == {id_evento}

    mensagens: list[dict[str, Any]] = []
    for _ in range(5):
        mensagens = (
            ambiente["sqs"]
            .receive_message(QueueUrl=ambiente["url"], MaxNumberOfMessages=10, WaitTimeSeconds=2)
            .get("Messages", [])
        )
        if mensagens:
            break
    assert len(mensagens) == 1
    envelope = json.loads(mensagens[0]["Body"])
    assert envelope["detail-type"] == "TransacaoAutorizada"
    assert envelope["source"] == "financas.autorizacao"
    assert envelope["detail"] == json.loads(cloudevent)

    restante = ddb.get_item(TableName=ambiente["tabela"], Key={"pk": {"S": f"OUTBOX#{id_evento}"}})
    assert "Item" not in restante
