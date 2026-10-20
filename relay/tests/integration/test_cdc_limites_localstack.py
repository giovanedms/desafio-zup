"""CDC de limites ponta a ponta na LocalStack: sincronizar (ACL) → Stream → relay → EventBridge → SQS."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB, definicao_da_tabela
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao
from limites.domain.limite import StatusContrato, TipoProduto
from relay.cdc_limites import SOURCE_NO_BUS, OutboxNoop, de_registro_do_stream_de_limites
from relay.eventbridge import PublicadorEventBridge
from relay.local import LeitorDeStream, cliente_local, uma_iteracao

pytestmark = pytest.mark.integracao

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")


@pytest.fixture
def ambiente() -> Iterator[dict[str, Any]]:
    dynamodb = cliente_local("dynamodb", ENDPOINT)
    streams = cliente_local("dynamodbstreams", ENDPOINT)
    events = cliente_local("events", ENDPOINT)
    sqs = cliente_local("sqs", ENDPOINT)
    sufixo = uuid.uuid4().hex[:8]
    tabela, bus, fila = f"limites-cdc-{sufixo}", f"bus-cdc-{sufixo}", f"fila-cdc-{sufixo}"

    dynamodb.create_table(**definicao_da_tabela(tabela))
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
        EventPattern=json.dumps({"source": [SOURCE_NO_BUS], "detail-type": ["LimiteAlterado"]}),
    )
    events.put_targets(Rule="r", EventBusName=bus, Targets=[{"Id": "t", "Arn": arn_fila}])
    try:
        yield {
            "dynamodb": dynamodb,
            "tabela": tabela,
            "sqs": sqs,
            "url": url,
            "events": events,
            "bus": bus,
            "leitor": LeitorDeStream(streams, arn_stream, de_registro_do_stream_de_limites),
        }
    finally:
        events.remove_targets(Rule="r", EventBusName=bus, Ids=["t"])
        events.delete_rule(Name="r", EventBusName=bus)
        events.delete_event_bus(Name=bus)
        sqs.delete_queue(QueueUrl=url)
        dynamodb.delete_table(TableName=tabela)


def atualizacao(versao: int, concedido: str) -> AtualizacaoDoLegado:
    return AtualizacaoDoLegado(
        "CTR-CDC",
        "0001-1",
        TipoProduto.CREDITO,
        Decimal(concedido),
        StatusContrato.ATIVO,
        versao,
        datetime.now(UTC),
    )


def test_atualizacao_do_legado_vira_limite_alterado_na_fila(ambiente: dict[str, Any]) -> None:
    repositorio = RepositorioDeLimitesDynamoDB(ambiente["dynamodb"], ambiente["tabela"])
    assert repositorio.sincronizar(atualizacao(1, "1000.00")) is ResultadoDaSincronizacao.CRIADO
    assert repositorio.sincronizar(atualizacao(2, "2500.00")) is ResultadoDaSincronizacao.ATUALIZADO
    publicador = PublicadorEventBridge(ambiente["events"], ambiente["bus"], source=SOURCE_NO_BUS)

    publicados: set[str] = set()
    for _ in range(20):  # o Stream da LocalStack pode demorar a expor o registro
        publicados |= uma_iteracao(ambiente["leitor"], publicador, OutboxNoop()).publicados
        if publicados:
            break
        time.sleep(0.5)
    assert len(publicados) == 1  # o INSERT da criação não gera evento; só a atualização

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
    assert envelope["detail-type"] == "LimiteAlterado"
    assert envelope["source"] == SOURCE_NO_BUS
    data = envelope["detail"]["data"]
    assert (data["limite_concedido_anterior"], data["limite_concedido"]) == (1000, 2500)
    assert envelope["detail"]["subject"] == "CTR-CDC"
    assert envelope["detail"]["id"] == publicados.pop()
