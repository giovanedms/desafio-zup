"""Entrada Lambda do relay: item malformado volta como falha do lote, e os clientes só falam com a AWS."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from autorizacao.adapters.outbound.dynamodb import _item_do_outbox
from autorizacao.domain.eventos import SaldosDoLimite, TransacaoAutorizada
from relay.nucleo import EventoPendente

from relay import lambda_handler


def evento(id_evento: str) -> TransacaoAutorizada:
    return TransacaoAutorizada(
        id_evento=id_evento,
        ocorrido_em=datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
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
            Decimal("1000.00"), Decimal("150.00"), Decimal("0.00"), Decimal("850.00"), 2
        ),
    )


BOM = evento("11111111-1111-4111-8111-111111111111")
OUTRO = evento("22222222-2222-4222-8222-222222222222")


class PublicadorFalso:
    def __init__(self, falhar: set[str] | None = None) -> None:
        self.falhar = falhar or set()
        self.publicados: list[str] = []

    def publicar(self, lote: Sequence[EventoPendente]) -> set[str]:
        ok = {e.id_evento for e in lote} - self.falhar
        self.publicados += sorted(ok)
        return ok


class OutboxFalso:
    def __init__(self) -> None:
        self.apagados: list[str] = []

    def apagar(self, ids: Iterable[str]) -> None:
        self.apagados += sorted(ids)

    def paginas_antigas(self, antes_de_ms: int) -> Iterator[list[EventoPendente]]:
        return iter(())


def _insert(imagem: dict[str, Any], seq: str) -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "eventSource": "aws:dynamodb",
        "dynamodb": {"SequenceNumber": seq, "Keys": {"pk": imagem["pk"]}, "NewImage": imagem},
    }


def _sem(campo: str, imagem: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in imagem.items() if k != campo}


@pytest.fixture
def falsos(monkeypatch: pytest.MonkeyPatch) -> tuple[PublicadorFalso, OutboxFalso]:
    publicador, outbox = PublicadorFalso(), OutboxFalso()
    monkeypatch.setattr(
        lambda_handler,
        "_componentes",
        lambda_handler.Componentes(publicador, outbox),  # type: ignore[arg-type]
    )
    return publicador, outbox


def test_item_malformado_volta_como_falha_e_o_resto_do_lote_sai(
    falsos: tuple[PublicadorFalso, OutboxFalso], caplog: pytest.LogCaptureFixture
) -> None:
    """F7-5/B-3/C7-2: o ESM retenta o malformado e, esgotado, manda para a DLQ on-failure (com alarme)."""
    publicador, outbox = falsos
    ruim = _insert(_sem("detail_type", _item_do_outbox(OUTRO)), "101")

    with caplog.at_level(logging.ERROR, logger="relay.lambda_handler"):
        resposta = lambda_handler.handler(
            {"Records": [_insert(_item_do_outbox(BOM), "100"), ruim]}, None
        )

    assert resposta == {"batchItemFailures": [{"itemIdentifier": "101"}]}
    assert publicador.publicados == outbox.apagados == [BOM.id_evento]
    linhas = [json.loads(r.getMessage()) for r in caplog.records if r.levelno == logging.ERROR]
    assert [(x["evento"], x["pk"], x["sequencia"]) for x in linhas] == [
        ("outbox_registro_malformado", f"OUTBOX#{OUTRO.id_evento}", "101")
    ]


def test_falha_de_publicacao_e_malformado_voltam_juntos(
    falsos: tuple[PublicadorFalso, OutboxFalso],
) -> None:
    publicador, _ = falsos
    publicador.falhar = {BOM.id_evento}
    ruim = _insert(_sem("cloudevent", _item_do_outbox(OUTRO)), "201")

    resposta = lambda_handler.handler(
        {"Records": [_insert(_item_do_outbox(BOM), "200"), ruim]}, None
    )

    assert resposta == {"batchItemFailures": [{"itemIdentifier": "200"}, {"itemIdentifier": "201"}]}


def test_lote_valido_nao_devolve_falha(falsos: tuple[PublicadorFalso, OutboxFalso]) -> None:
    registros = [_insert(_item_do_outbox(e), s) for e, s in ((BOM, "300"), (OUTRO, "301"))]

    assert lambda_handler.handler({"Records": registros}, None) == {"batchItemFailures": []}


@pytest.mark.parametrize(
    "variavel", ["AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_EVENTBRIDGE", "AWS_ENDPOINT_URL_DYNAMODB"]
)
def test_endpoint_de_variavel_de_ambiente_nao_desvia_os_clientes(
    monkeypatch: pytest.MonkeyPatch, variavel: str
) -> None:
    """G7-1: uma variável na configuração da função não manda o fluxo de eventos para outro host."""
    monkeypatch.setenv(variavel, "https://remoto.exemplo.com")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")
    monkeypatch.setenv("BUS", "financas")
    monkeypatch.setenv("TABELA_OUTBOX", "autorizacoes-outbox")
    monkeypatch.setattr(lambda_handler, "_componentes", None)
    monkeypatch.setattr(lambda_handler, "_cdc_limites", None)

    relay = lambda_handler.componentes()  # relay e sweeper
    cdc = lambda_handler.componentes_cdc_limites()

    assert relay.publicador._cliente.meta.endpoint_url == "https://events.sa-east-1.amazonaws.com"
    assert relay.outbox._cliente.meta.endpoint_url == "https://dynamodb.sa-east-1.amazonaws.com"
    assert cdc.publicador._cliente.meta.endpoint_url == "https://events.sa-east-1.amazonaws.com"
