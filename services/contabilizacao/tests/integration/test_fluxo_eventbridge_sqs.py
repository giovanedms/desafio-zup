"""Fluxo real na LocalStack: PutEvents → regra → SQS → poller → lançamento no DynamoDB.

Prova que o número exato sobrevive ao caminho EventBridge → SQS → json.loads(parse_float=Decimal).
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from contabilizacao.adapters.inbound.poller import uma_iteracao
from contabilizacao.adapters.outbound.dynamodb import RepositorioContabilDynamoDB
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.domain.regras import LivroDeRegras

pytestmark = pytest.mark.integracao

EXEMPLO = next(
    p / "contracts/events/examples/transacao-autorizada.v1.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/events/examples/transacao-autorizada.v1.json").exists()
)
VALOR = "12345678901234.56"


def test_numero_exato_do_eventbridge_ate_o_lancamento(
    cliente_dynamodb_local: Any,
    cliente_sqs_local: Any,
    cliente_events_local: Any,
    tabelas_contabilizacao: tuple[str, str],
) -> None:
    sufixo = uuid.uuid4().hex[:8]
    bus, fila = f"bus-teste-{sufixo}", f"fila-teste-{sufixo}"
    events, sqs = cliente_events_local, cliente_sqs_local
    events.create_event_bus(Name=bus)
    url = sqs.create_queue(QueueName=fila)["QueueUrl"]
    try:
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        events.put_rule(
            Name="regra",
            EventBusName=bus,
            EventPattern=json.dumps({"detail-type": ["TransacaoAutorizada"]}),
        )
        events.put_targets(Rule="regra", EventBusName=bus, Targets=[{"Id": "fila", "Arn": arn}])

        detail = EXEMPLO.read_text().replace('"valor": 150.0,', f'"valor": {VALOR},')
        assert VALOR in detail
        events.put_events(
            Entries=[
                {
                    "Source": "financas.autorizacao",
                    "DetailType": "TransacaoAutorizada",
                    "Detail": detail,
                    "EventBusName": bus,
                }
            ]
        )

        # espia sem consumir (VisibilityTimeout=0) para registrar o corpo bruto
        corpo = ""
        for _ in range(20):
            msgs = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=1, VisibilityTimeout=0
            ).get("Messages", [])
            if msgs:
                corpo = msgs[0]["Body"]
                break
            time.sleep(0.5)
        assert corpo, "mensagem não chegou na fila"
        i = corpo.index('"valor"')
        print(f"\nCORPO BRUTO SQS (trecho com o valor): ...{corpo[i - 20 : i + 80]}...")

        lancamentos, inbox = tabelas_contabilizacao
        repo = RepositorioContabilDynamoDB(cliente_dynamodb_local, lancamentos, inbox)
        desfechos = uma_iteracao(sqs, url, ContabilizarEvento(repo, LivroDeRegras()), espera_s=2)

        assert desfechos == {"CONTABILIZADO": 1}
        (item,) = repo.listar(10)
        assert item["partidas"][0]["valor"] == Decimal(VALOR)
        assert item["partidas"][1]["valor"] == Decimal(VALOR)
    finally:
        sqs.delete_queue(QueueUrl=url)
        events.remove_targets(Rule="regra", EventBusName=bus, Ids=["fila"])
        events.delete_rule(Name="regra", EventBusName=bus)
        events.delete_event_bus(Name=bus)
