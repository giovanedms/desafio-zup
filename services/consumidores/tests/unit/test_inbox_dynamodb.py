"""Adapter DynamoDB da inbox: a escrita condicional que decide NOVO / CONCLUIDO / EM_ANDAMENTO (Stubber, sem rede)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from consumidores.inbox_dynamodb import InboxDynamoDB
from consumidores.nucleo import Situacao

AGORA_MS = 1_790_000_000_123
PRAZO_MS = AGORA_MS + 30_000
CHAVE = "/financas/autorizacao|a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
NOMES = {"#estado": "estado", "#prazo": "prazo_ms"}


@pytest.fixture
def stub() -> Iterator[tuple[InboxDynamoDB, Stubber]]:
    cliente: Any = boto3.client(
        "dynamodb", region_name="sa-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(cliente) as stubber:
        yield InboxDynamoDB(cliente, "consumidor-inbox"), stubber
        stubber.assert_no_pending_responses()


def _put_esperado() -> dict[str, Any]:
    return {
        "TableName": "consumidor-inbox",
        "Item": {
            "pk": {"S": f"INBOX#{CHAVE}"},
            "estado": {"S": "EM_PROCESSAMENTO"},
            "prazo_ms": {"N": str(PRAZO_MS)},
            "ttl": {"N": str(AGORA_MS // 1000 + 30 * 86_400)},  # 30 dias
        },
        "ConditionExpression": "attribute_not_exists(pk) OR (#estado = :em_processamento AND #prazo < :agora)",
        "ExpressionAttributeNames": NOMES,
        "ExpressionAttributeValues": {
            ":em_processamento": {"S": "EM_PROCESSAMENTO"},
            ":agora": {"N": str(AGORA_MS)},
        },
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }


def test_chave_nova_e_reivindicada_com_prazo_e_ttl(stub: tuple[InboxDynamoDB, Stubber]) -> None:
    inbox, stubber = stub
    stubber.add_response("put_item", {}, _put_esperado())

    assert inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS) is Situacao.NOVO


@pytest.mark.parametrize(
    ("estado", "situacao"),
    [("CONCLUIDO", Situacao.CONCLUIDO), ("EM_PROCESSAMENTO", Situacao.EM_ANDAMENTO)],
)
def test_condicao_falha_e_o_item_devolvido_decide(
    stub: tuple[InboxDynamoDB, Stubber], estado: str, situacao: Situacao
) -> None:
    inbox, stubber = stub
    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=_put_esperado(),
        modeled_fields={"Item": {"pk": {"S": f"INBOX#{CHAVE}"}, "estado": {"S": estado}}},
    )

    assert inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS) is situacao


def test_outro_erro_do_dynamodb_sobe(stub: tuple[InboxDynamoDB, Stubber]) -> None:
    inbox, stubber = stub
    stubber.add_client_error(
        "put_item", service_error_code="ProvisionedThroughputExceededException"
    )

    with pytest.raises(ClientError):
        inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS)


def test_concluir_marca_concluido_e_tira_o_prazo(stub: tuple[InboxDynamoDB, Stubber]) -> None:
    inbox, stubber = stub
    stubber.add_response(
        "update_item",
        {},
        {
            "TableName": "consumidor-inbox",
            "Key": {"pk": {"S": f"INBOX#{CHAVE}"}},
            "UpdateExpression": "SET #estado = :concluido REMOVE #prazo",
            "ConditionExpression": "attribute_exists(pk)",
            "ExpressionAttributeNames": NOMES,
            "ExpressionAttributeValues": {":concluido": {"S": "CONCLUIDO"}},
        },
    )

    inbox.concluir(CHAVE)
