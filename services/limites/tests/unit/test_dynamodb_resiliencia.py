"""Adapter DynamoDB sob estrangulamento e o sinal de contrato quente (sem rede: botocore Stubber)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from limites.adapters.outbound.dynamodb import (
    LIMIAR_DE_ALERTA_DA_GUARDA,
    ArmazenamentoIndisponivel,
    RepositorioDeLimitesDynamoDB,
    _para_item,
)
from limites.domain.limite import Limite, TipoProduto
from limites.domain.operacoes import Debitar

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def _cliente() -> tuple[object, Stubber]:
    # Credenciais fictícias explícitas: o Stubber responde antes de qualquer chamada de rede.
    cliente = boto3.client(
        "dynamodb",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 1}),
    )
    return cliente, Stubber(cliente)


@pytest.mark.parametrize(
    "codigo",
    [
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "InternalServerError",
    ],
)
def test_estrangulamento_vira_armazenamento_indisponivel(codigo: str) -> None:
    cliente, stub = _cliente()
    stub.add_client_error("update_item", service_error_code=codigo, http_status_code=400)

    with stub, pytest.raises(ArmazenamentoIndisponivel):
        RepositorioDeLimitesDynamoDB(cliente, "limites").aplicar(  # type: ignore[arg-type]
            "CTR-1", Debitar("d1", Decimal("1.00")), AGORA
        )


def test_erro_que_nao_e_transitorio_continua_sendo_erro() -> None:
    cliente, stub = _cliente()
    stub.add_client_error("update_item", service_error_code="ValidationException")

    with stub, pytest.raises(ClientError):
        RepositorioDeLimitesDynamoDB(cliente, "limites").aplicar(  # type: ignore[arg-type]
            "CTR-1", Debitar("d1", Decimal("1.00")), AGORA
        )


def _limite_com_guarda(tamanho: int) -> Limite:
    limite = Limite.novo("CTR-QUENTE", "0001-1", TipoProduto.CREDITO, Decimal("1000000.00"))
    for i in range(tamanho):
        limite = limite.aplicar(Debitar(f"d{i}", Decimal("0.01")).planejar(), AGORA)
    return limite


@pytest.mark.parametrize(
    ("tamanho", "loga"),
    [(LIMIAR_DE_ALERTA_DA_GUARDA - 2, False), (LIMIAR_DE_ALERTA_DA_GUARDA, True)],
)
def test_guarda_grande_vira_linha_json_para_o_alarme(
    caplog: pytest.LogCaptureFixture, tamanho: int, loga: bool
) -> None:
    anterior = _limite_com_guarda(tamanho - 1)
    depois = anterior.aplicar(Debitar("ultima", Decimal("0.01")).planejar(), AGORA)
    cliente, stub = _cliente()
    stub.add_response("update_item", {"Attributes": _para_item(depois)})

    with stub, caplog.at_level(logging.WARNING, logger="limites.guarda"):
        RepositorioDeLimitesDynamoDB(cliente, "limites").aplicar(  # type: ignore[arg-type]
            "CTR-QUENTE", Debitar("ultima", Decimal("0.01")), AGORA
        )

    linhas = [r.getMessage() for r in caplog.records if r.name == "limites.guarda"]
    if not loga:
        assert linhas == []
        return
    evento = json.loads(linhas[0])
    assert evento == {
        "evento": "guarda_de_idempotencia",
        "id_contrato": "CTR-QUENTE",
        "operacoes_na_guarda": tamanho,
        "reservas_ativas": 0,
    }
