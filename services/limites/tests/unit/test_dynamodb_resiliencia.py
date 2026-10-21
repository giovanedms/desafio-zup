"""Adapter DynamoDB sob estrangulamento e o sinal de contrato quente (sem rede: botocore Stubber)."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

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
    compilar,
)
from limites.domain.limite import JANELA_DE_DEDUPLICACAO, Limite, TipoProduto
from limites.domain.operacoes import Debitar
from limites.domain.resultados import Desfecho

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


def test_muitas_reservas_com_guarda_pequena_tambem_vira_linha_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Holds vivem até 7 dias no item: o contrato chega aos 400 KB com a guarda quase vazia.
    reservas = {f"r{i}": Decimal("1.00") for i in range(LIMIAR_DE_ALERTA_DA_GUARDA)}
    base = Limite.novo("CTR-HOTEL", "0001-1", TipoProduto.CREDITO, Decimal("1000000.00"))
    depois = replace(
        base,
        reservado=Decimal(len(reservas)),
        disponivel=base.concedido - Decimal(len(reservas)),
        reservas=MappingProxyType(reservas),
        operacoes_recentes=MappingProxyType({"ultima": AGORA}),
    )
    cliente, stub = _cliente()
    stub.add_response("update_item", {"Attributes": _para_item(depois)})

    with stub, caplog.at_level(logging.WARNING, logger="limites.guarda"):
        RepositorioDeLimitesDynamoDB(cliente, "limites").aplicar(  # type: ignore[arg-type]
            "CTR-HOTEL", Debitar("ultima", Decimal("0.01")), AGORA
        )

    linhas = [r.getMessage() for r in caplog.records if r.name == "limites.guarda"]
    assert json.loads(linhas[0]) == {
        "evento": "guarda_de_idempotencia",
        "id_contrato": "CTR-HOTEL",
        "operacoes_na_guarda": 1,
        "reservas_ativas": LIMIAR_DE_ALERTA_DA_GUARDA,
    }


def _escrita(id_contrato: str, plano_e_remocoes: dict[str, object]) -> dict[str, object]:
    return {
        "TableName": "limites",
        "Key": {"id_contrato": {"S": id_contrato}},
        "ReturnValues": "ALL_NEW",
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        **plano_e_remocoes,
    }


def test_remove_de_carona_que_acha_entrada_fresca_repete_a_escrita_sem_compactar() -> None:
    """F-5, sequência exata de chamadas: esta task viu `d-velho` vencido; outra o reaplicou (entrada fresca).

    A 1ª escrita leva o REMOVE condicionado e falha só por ele; a 2ª vai sem REMOVE e aplica. Sem a condição,
    o REMOVE apagaria a entrada fresca e um retry de `d-velho` dentro da janela debitaria de novo.
    """
    t0 = AGORA
    t1 = t0 + JANELA_DE_DEDUPLICACAO + timedelta(minutes=1)
    base = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, Decimal("1000.00"))
    com_velho = base.aplicar(Debitar("d-velho", Decimal("1.00")).planejar(), t0)
    visto = com_velho.aplicar(Debitar("x", Decimal("1.00")).planejar(), t1)  # d-velho vencido aqui
    reaplicado = replace(  # outra task compactou e reaplicou d-velho em t1
        visto,
        operacoes_recentes=MappingProxyType({"x": t1, "d-velho": t1}),
        utilizado=visto.utilizado + 1,
        disponivel=visto.disponivel - 1,
        versao=visto.versao + 2,
    )
    w = Debitar("w", Decimal("1.00"))
    depois = reaplicado.aplicar(w.planejar(), t1)
    cliente, stub = _cliente()
    stub.add_response(
        "update_item",
        {"Attributes": _para_item(visto)},
        _escrita("CTR-1", compilar(Debitar("x", Decimal("1.00")).planejar(), t1)),
    )
    stub.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=_escrita("CTR-1", compilar(w.planejar(), t1, ("d-velho",))),
        modeled_fields={"Item": _para_item(reaplicado)},
    )
    stub.add_response(
        "update_item",
        {"Attributes": _para_item(depois)},
        _escrita("CTR-1", compilar(w.planejar(), t1)),
    )
    repositorio = RepositorioDeLimitesDynamoDB(cliente, "limites")  # type: ignore[arg-type]

    with stub:
        repositorio.aplicar("CTR-1", Debitar("x", Decimal("1.00")), t1)
        resultado = repositorio.aplicar("CTR-1", w, t1)

    stub.assert_no_pending_responses()
    assert resultado.desfecho is Desfecho.APLICADA
