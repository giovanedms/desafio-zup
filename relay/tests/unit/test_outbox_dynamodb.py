"""Conversão de registros do Streams e operações do outbox (com Stubber).

O item válido é o que o produtor REAL grava (`_item_do_outbox` do BC Autorização), não um dicionário escrito à mão:
se o layout do item mudar de um lado só, este teste fica vermelho em vez de o relay descartar o evento.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest
from autorizacao.adapters.outbound.cloudevents import para_cloudevent, serializar
from autorizacao.adapters.outbound.dynamodb import _item_do_outbox
from autorizacao.domain.eventos import SaldosDoLimite, TransacaoAutorizada
from botocore.stub import Stubber
from relay.outbox_dynamodb import ItemDeOutboxMalformado, OutboxDynamoDB, de_registro_do_stream

OCORRIDO_EM = datetime(2026, 10, 20, 13, 0, 0, 345_000, tzinfo=UTC)


def evento(id_evento: str = "7a1c9e2b-4d3f-4a8e-9b6c-1f2e3d4c5b6a") -> TransacaoAutorizada:
    return TransacaoAutorizada(
        id_evento=id_evento,
        ocorrido_em=OCORRIDO_EM,
        id_contrato="CTR-000123456",
        correlation_id="0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b",
        traceparent=None,
        id_autorizacao="5f4e3d2c-1b0a-4f9e-8d7c-6b5a4f3e2d1c",
        tipo_operacao="DEBITO",
        valor=Decimal("150.00"),
        moeda="BRL",
        id_conta="0001-00000028-9",
        id_estabelecimento=None,
        saldos=SaldosDoLimite(
            limite_concedido=Decimal("1000.00"),
            limite_utilizado=Decimal("150.00"),
            saldo_reservado=Decimal("0.00"),
            limite_disponivel=Decimal("850.00"),
            versao=2,
        ),
    )


EVENTO = evento()
IMAGEM: dict[str, Any] = _item_do_outbox(EVENTO)


def _insert(imagem: dict[str, Any] | None, seq: str = "1") -> dict[str, Any]:
    corpo: dict[str, Any] = {"SequenceNumber": seq, "Keys": {"pk": IMAGEM["pk"]}}
    if imagem is not None:
        corpo["NewImage"] = imagem
    return {"eventID": "x", "eventName": "INSERT", "eventSource": "aws:dynamodb", "dynamodb": corpo}


def test_item_real_do_produtor_vira_evento_pendente_formato_lambda() -> None:
    pendente = de_registro_do_stream(_insert(IMAGEM))

    assert pendente is not None
    assert pendente.id_evento == EVENTO.id_evento
    assert pendente.detail_type == "TransacaoAutorizada"
    assert pendente.cloudevent == serializar(
        para_cloudevent(EVENTO)
    )  # string intacta, sem reserializar
    assert pendente.criado_em_ms == int(OCORRIDO_EM.timestamp() * 1000)


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


def test_pk_fora_do_outbox_vira_none() -> None:
    outro = {**IMAGEM, "pk": {"S": "AUT#1"}}
    assert de_registro_do_stream(_insert(outro)) is None


@pytest.mark.parametrize(
    "estragar",
    [
        pytest.param(lambda i: i.pop("id_evento"), id="sem-id_evento"),
        pytest.param(lambda i: i.pop("detail_type"), id="sem-detail_type"),
        pytest.param(lambda i: i.pop("cloudevent"), id="sem-cloudevent"),
        pytest.param(lambda i: i.pop("criado_em_ms"), id="sem-criado_em_ms"),
        pytest.param(lambda i: i.update(criado_em_ms={"N": "abc"}), id="criado_em_ms-invalido"),
        pytest.param(lambda i: i.update(detail_type={"X": "?"}), id="tipo-dynamodb-invalido"),
    ],
)
def test_item_do_outbox_malformado_levanta_com_o_pk(estragar: Any) -> None:
    """F7-5/B-3/C7-2: o malformado não vira None (o handler o confirmaria ao ESM e ele sumiria)."""
    imagem = dict(IMAGEM)
    estragar(imagem)

    with pytest.raises(ItemDeOutboxMalformado) as erro:
        de_registro_do_stream(_insert(imagem))

    assert erro.value.pk == f"OUTBOX#{EVENTO.id_evento}"


def test_item_sem_pk_levanta() -> None:
    imagem = {k: v for k, v in IMAGEM.items() if k != "pk"}

    with pytest.raises(ItemDeOutboxMalformado) as erro:
        de_registro_do_stream(_insert(imagem))

    assert erro.value.pk is None


def test_insert_sem_new_image_levanta_com_o_pk_das_chaves() -> None:
    """Stream sem NEW_IMAGE: todo evento sumiria em silêncio; aqui cada registro volta como falha."""
    with pytest.raises(ItemDeOutboxMalformado, match="sem NewImage") as erro:
        de_registro_do_stream(_insert(None))

    assert erro.value.pk == f"OUTBOX#{EVENTO.id_evento}"


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


BASE_DO_SCAN = {
    "TableName": "t",
    "Limit": 2,
    "ProjectionExpression": "pk, id_evento, detail_type, cloudevent, criado_em_ms",
    "FilterExpression": "criado_em_ms < :t",
    "ExpressionAttributeValues": {":t": {"N": "5"}},
}


def test_paginas_antigas_usa_limit_e_entrega_uma_pagina_por_vez() -> None:
    cliente = _cliente_dynamodb()
    chave = {"pk": IMAGEM["pk"]}
    with Stubber(cliente) as stub:
        stub.add_response("scan", {"Items": [IMAGEM], "LastEvaluatedKey": chave}, BASE_DO_SCAN)
        stub.add_response("scan", {"Items": []}, {**BASE_DO_SCAN, "ExclusiveStartKey": chave})
        paginas = OutboxDynamoDB(cliente, "t").paginas_antigas(5, itens_por_pagina=2)
        assert [e.id_evento for e in next(paginas)] == [EVENTO.id_evento]
        assert next(paginas) == []  # página sem item antigo também sai (o sweeper confere o prazo)
        assert next(paginas, None) is None
        stub.assert_no_pending_responses()


def test_sweeper_loga_o_malformado_com_o_pk_e_publica_o_resto_da_pagina(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """O item malformado fica na tabela; cada rodada do sweeper o loga (termo do metric filter) em vez de pular mudo."""
    outro = evento("3c2b1a0f-9e8d-4c7b-a6f5-e4d3c2b1a0f9")
    ruim = {k: v for k, v in _item_do_outbox(outro).items() if k != "detail_type"}
    cliente = _cliente_dynamodb()
    with Stubber(cliente) as stub:
        stub.add_response("scan", {"Items": [ruim, IMAGEM]}, BASE_DO_SCAN)
        with caplog.at_level(logging.ERROR, logger="relay.outbox_dynamodb"):
            pagina = next(OutboxDynamoDB(cliente, "t").paginas_antigas(5, itens_por_pagina=2))

    assert [e.id_evento for e in pagina] == [EVENTO.id_evento]
    linhas = [json.loads(r.getMessage()) for r in caplog.records]
    assert [(x["evento"], x["pk"]) for x in linhas] == [
        ("outbox_registro_malformado", f"OUTBOX#{outro.id_evento}")
    ]
