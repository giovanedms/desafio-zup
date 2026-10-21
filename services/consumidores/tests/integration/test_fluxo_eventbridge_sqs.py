"""Fluxo real na LocalStack: PutEvents → regra → SQS → handler da Lambda → inbox no DynamoDB.

Prova que o corpo que o EventBridge entrega na fila é o envelope que o consumidor lê (`detail-type`, `detail`), que o
mesmo CloudEvent publicado duas vezes (relay e sweeper) sai uma vez só, e que o inválido volta como falha do lote.
Bus, regra e fila com nome próprio (sufixo aleatório), apagados no fim; nada da demo é tocado.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from consumidores import lambda_sqs
from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.inbox_dynamodb import InboxDynamoDB
from consumidores.nucleo import ConsumirEvento

pytestmark = pytest.mark.integracao

EXEMPLO = json.loads(
    next(
        p / "contracts/events/examples/transacao-autorizada.v1.json"
        for p in Path(__file__).resolve().parents
        if (p / "contracts/events/examples/transacao-autorizada.v1.json").exists()
    ).read_text()
)


@pytest.fixture
def fila_assinada(cliente_events_local: Any, cliente_sqs_local: Any) -> Iterator[tuple[str, str]]:
    """Bus + regra (TransacaoAutorizada) + fila própria: (nome do bus, URL da fila)."""
    events, sqs = cliente_events_local, cliente_sqs_local
    sufixo = uuid.uuid4().hex[:8]
    bus = f"bus-consumidor-teste-{sufixo}"
    events.create_event_bus(Name=bus)
    url = sqs.create_queue(QueueName=f"fila-consumidor-teste-{sufixo}")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    events.put_rule(
        Name="regra",
        EventBusName=bus,
        EventPattern=json.dumps(
            {"source": ["financas.autorizacao"], "detail-type": ["TransacaoAutorizada"]}
        ),
    )
    events.put_targets(Rule="regra", EventBusName=bus, Targets=[{"Id": "fila", "Arn": arn}])
    try:
        yield bus, url
    finally:
        events.remove_targets(Rule="regra", EventBusName=bus, Ids=["fila"])
        events.delete_rule(Name="regra", EventBusName=bus)
        events.delete_event_bus(Name=bus)
        sqs.delete_queue(QueueUrl=url)


def _publicar(events: Any, bus: str, cloudevent: dict[str, Any]) -> None:
    resposta = events.put_events(
        Entries=[
            {
                "Source": "financas.autorizacao",
                "DetailType": "TransacaoAutorizada",
                "Detail": json.dumps(cloudevent),
                "EventBusName": bus,
            }
        ]
    )
    assert resposta["FailedEntryCount"] == 0


def _receber(sqs: Any, url: str, quantas: int) -> list[dict[str, Any]]:
    mensagens: list[dict[str, Any]] = []
    for _ in range(30):
        mensagens += sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1
        ).get("Messages", [])
        if len(mensagens) >= quantas:
            break
        time.sleep(0.3)
    return mensagens


def test_do_eventbridge_a_linha_com_duplicata_e_invalido(
    *,
    monkeypatch: pytest.MonkeyPatch,
    catalogo: CatalogoDeEsquemas,
    cliente_dynamodb_local: Any,
    cliente_events_local: Any,
    cliente_sqs_local: Any,
    tabela_inbox: str,
    fila_assinada: tuple[str, str],
) -> None:
    bus, url = fila_assinada
    valido = copy.deepcopy(EXEMPLO)
    valido["id"] = valido["data"]["event_id"] = str(uuid.uuid4())
    invalido = copy.deepcopy(valido)
    invalido["id"] = invalido["data"]["event_id"] = str(uuid.uuid4())
    invalido["data"]["valor"] = -1
    for ce in (
        valido,
        valido,
        invalido,
    ):  # o válido duas vezes: relay e sweeper publicando o mesmo evento
        _publicar(cliente_events_local, bus, ce)

    mensagens = _receber(cliente_sqs_local, url, 3)
    assert len(mensagens) == 3, "as 3 publicações deviam chegar na fila"
    linhas: list[dict[str, Any]] = []
    monkeypatch.setattr(
        lambda_sqs,
        "_consumir",
        ConsumirEvento(
            "antifraude",
            catalogo,
            InboxDynamoDB(cliente_dynamodb_local, tabela_inbox),
            linhas.append,
            lambda: time.time_ns() // 1_000_000,
        ),
    )
    id_da_mensagem_invalida = next(
        m["MessageId"] for m in mensagens if json.loads(m["Body"])["detail"]["id"] == invalido["id"]
    )

    resposta = lambda_sqs.handler(
        {"Records": [{"messageId": m["MessageId"], "body": m["Body"]} for m in mensagens]}, None
    )

    assert resposta == {"batchItemFailures": [{"itemIdentifier": id_da_mensagem_invalida}]}
    assert [linha["id"] for linha in linhas] == [valido["id"]]
    assert linhas[0]["id_autorizacao"] == valido["data"]["id_autorizacao"]
