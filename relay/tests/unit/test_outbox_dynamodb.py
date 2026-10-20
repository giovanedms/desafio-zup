"""Conversão de registros do Streams e operações do outbox (com Stubber)."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from relay.outbox_dynamodb import OutboxDynamoDB, de_registro_do_stream

IMAGEM: dict[str, Any] = {
    "pk": {"S": "OUTBOX#ev-1"},
    "id_evento": {"S": "ev-1"},
    "detail_type": {"S": "TransacaoAutorizada"},
    "cloudevent": {"S": '{"data":{"valor":150.00}}'},
    "id_contrato": {"S": "CTR-1"},
    "criado_em_ms": {"N": "1760967912345"},
    "ttl": {"N": "1761572712"},
}


def test_insert_vira_evento_pendente_formato_lambda() -> None:
    registro = {
        "eventID": "x",
        "eventName": "INSERT",
        "eventSource": "aws:dynamodb",
        "dynamodb": {"NewImage": IMAGEM, "SequenceNumber": "1"},
    }
    pendente = de_registro_do_stream(registro)
    assert pendente is not None
    assert pendente.id_evento == "ev-1"
    assert pendente.detail_type == "TransacaoAutorizada"
    assert pendente.cloudevent == '{"data":{"valor":150.00}}'  # string intacta
    assert pendente.criado_em_ms == 1760967912345


def test_insert_vira_evento_pendente_formato_get_records() -> None:
    registro = {
        "eventID": "x",
        "eventName": "INSERT",
        "eventVersion": "1.1",
        "dynamodb": {
            "Keys": {"pk": IMAGEM["pk"]},
            "NewImage": IMAGEM,
            "StreamViewType": "NEW_IMAGE",
        },
    }
    assert de_registro_do_stream(registro) is not None


@pytest.mark.parametrize("nome", ["REMOVE", "MODIFY"])
def test_remove_e_modify_viram_none(nome: str) -> None:
    assert de_registro_do_stream({"eventName": nome, "dynamodb": {"NewImage": IMAGEM}}) is None


def test_pk_fora_do_outbox_ou_malformado_vira_none() -> None:
    outro = {**IMAGEM, "pk": {"S": "AUT#1"}}
    assert de_registro_do_stream({"eventName": "INSERT", "dynamodb": {"NewImage": outro}}) is None
    sem_cloudevent = {k: v for k, v in IMAGEM.items() if k != "cloudevent"}
    registro = {"eventName": "INSERT", "dynamodb": {"NewImage": sem_cloudevent}}
    assert de_registro_do_stream(registro) is None


def _cliente_dynamodb() -> Any:
    return boto3.client(
        "dynamodb",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_apagar_em_lotes_de_25_e_reprocessa_nao_processados() -> None:
    cliente = _cliente_dynamodb()
    ids = [f"ev-{i}" for i in range(30)]
    with Stubber(cliente) as stub:
        pendente = {"DeleteRequest": {"Key": {"pk": {"S": "OUTBOX#ev-3"}}}}
        stub.add_response("batch_write_item", {"UnprocessedItems": {"t": [pendente]}})
        stub.add_response("batch_write_item", {"UnprocessedItems": {}})  # reprocesso do 1o lote
        stub.add_response("batch_write_item", {"UnprocessedItems": {}})  # 2o lote (5 itens)
        OutboxDynamoDB(cliente, "t").apagar(ids)
        stub.assert_no_pending_responses()


def test_listar_antigos_pagina_e_filtra() -> None:
    cliente = _cliente_dynamodb()
    with Stubber(cliente) as stub:
        stub.add_response(
            "scan", {"Items": [IMAGEM], "LastEvaluatedKey": {"pk": {"S": "OUTBOX#ev-1"}}}
        )
        stub.add_response("scan", {"Items": []})
        antigos = OutboxDynamoDB(cliente, "t").listar_antigos(5)
        stub.assert_no_pending_responses()
    assert [e.id_evento for e in antigos] == ["ev-1"]
