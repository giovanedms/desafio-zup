from __future__ import annotations

import boto3
from botocore.stub import Stubber
from relay.eventbridge import PublicadorEventBridge
from relay.nucleo import EventoPendente

LOTE = [
    EventoPendente(f"ev-{i}", "TransacaoAutorizada", f'{{"valor":150.10,"i":{i}}}', 0)
    for i in range(3)
]


def _cliente() -> object:
    return boto3.client(
        "events", region_name="sa-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )


def test_falha_parcial_por_posicao_e_detail_sem_reserializar() -> None:
    cliente = _cliente()
    esperado = [
        {
            "Source": "financas.autorizacao",
            "DetailType": "TransacaoAutorizada",
            "Detail": e.cloudevent,
            "EventBusName": "financas",
        }
        for e in LOTE
    ]
    with Stubber(cliente) as stub:  # type: ignore[arg-type]
        stub.add_response(
            "put_events",
            {
                "FailedEntryCount": 1,
                "Entries": [
                    {"EventId": "a"},
                    {"ErrorCode": "InternalFailure", "ErrorMessage": "x"},
                    {"EventId": "c"},
                ],
            },
            {"Entries": esperado},
        )
        publicados = PublicadorEventBridge(cliente, "financas").publicar(LOTE)  # type: ignore[arg-type]
    assert publicados == {"ev-0", "ev-2"}


def test_excecao_do_cliente_nao_publica_nada() -> None:
    cliente = _cliente()
    with Stubber(cliente) as stub:  # type: ignore[arg-type]
        stub.add_client_error("put_events", service_error_code="ThrottlingException")
        assert PublicadorEventBridge(cliente, "financas").publicar(LOTE) == set()  # type: ignore[arg-type]


def test_lote_vazio_nao_chama_aws() -> None:
    assert PublicadorEventBridge(_cliente(), "financas").publicar([]) == set()  # type: ignore[arg-type]
