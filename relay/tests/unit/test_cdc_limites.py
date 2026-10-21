"""CDC da tabela limites: só concedido/status mudando gera LimiteAlterado, e o CloudEvent bate com o schema."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from relay.cdc_limites import (
    NOME_CURTO,
    OutboxNoop,
    RegistroMalformado,
    de_registro_do_stream_de_limites,
)
from relay.lambda_handler import handler_cdc_limites
from relay.nucleo import EventoPendente

SCHEMA = json.loads(
    next(
        p / "contracts/events/limite-alterado.v1.schema.json"
        for p in Path(__file__).resolve().parents
        if (p / "contracts/events/limite-alterado.v1.schema.json").exists()
    ).read_text()
)


def imagem(
    concedido: str = "1000.00",
    status: str = "ATIVO",
    *,
    disponivel: str = "1000.00",
    versao: int = 1,
    versao_legado: int = 1,
    reservado: str = "0.00",
) -> dict[str, Any]:
    return {
        "id_contrato": {"S": "CTR-1"},
        "concedido": {"N": concedido},
        "status": {"S": status},
        "disponivel": {"N": disponivel},
        "reservado": {"N": reservado},
        "utilizado": {"N": "0.00"},
        "versao": {"N": str(versao)},
        "versao_legado": {"N": str(versao_legado)},
    }


def registro(antes: dict[str, Any], depois: dict[str, Any], nome: str = "MODIFY") -> dict[str, Any]:
    return {
        "eventName": nome,
        "dynamodb": {
            "SequenceNumber": "100",
            "ApproximateCreationDateTime": 1792503912.345,
            "OldImage": antes,
            "NewImage": depois,
        },
    }


def alteracao() -> dict[str, Any]:
    return registro(
        imagem(),
        imagem("1500.00", disponivel="1500.00", versao=2, versao_legado=2),
    )


def test_aumento_de_limite_gera_evento_valido_no_schema() -> None:
    pendente = de_registro_do_stream_de_limites(alteracao())

    assert pendente is not None
    assert pendente.detail_type == NOME_CURTO
    ce = json.loads(pendente.cloudevent)
    jsonschema.Draft4Validator(SCHEMA).validate(ce)
    assert ce["source"] == "/financas/limites"
    assert ce["type"] == "example.financas.limites.LimiteAlterado.v1"
    assert ce["subject"] == ce["partitionkey"] == "CTR-1"
    assert ce["id"] == pendente.id_evento == ce["data"]["event_id"]
    assert ce["data"]["limite_concedido_anterior"] == 1000
    assert ce["data"]["limite_concedido"] == 1500
    assert ce["data"]["limite_disponivel"] == 1500
    assert (ce["data"]["versao_limite"], ce["data"]["versao_legado"]) == (2, 2)
    assert ce["data"]["origem"] == "CORE_LEGADO"
    assert ce["data"]["trace_id"] is None


def test_dinheiro_sai_como_numero_exato() -> None:
    pendente = de_registro_do_stream_de_limites(alteracao())

    assert pendente is not None
    assert '"limite_concedido":1500.00' in pendente.cloudevent


def test_mudanca_de_status_gera_evento() -> None:
    antes, depois = imagem(), imagem(status="BLOQUEADO", versao=2, versao_legado=3)

    pendente = de_registro_do_stream_de_limites(registro(antes, depois))

    assert pendente is not None
    ce = json.loads(pendente.cloudevent)
    jsonschema.Draft4Validator(SCHEMA).validate(ce)
    assert (ce["data"]["status_contrato_anterior"], ce["data"]["status_contrato"]) == (
        "ATIVO",
        "BLOQUEADO",
    )


def test_id_e_deterministico_para_o_mesmo_contrato_e_versao() -> None:
    a = de_registro_do_stream_de_limites(alteracao())
    b = de_registro_do_stream_de_limites(alteracao())

    assert a is not None
    assert b is not None
    assert a.id_evento == b.id_evento
    assert a.cloudevent == b.cloudevent


def test_reserva_e_debito_nao_geram_evento() -> None:
    reservou = imagem(disponivel="900.00", reservado="100.00", versao=2)

    assert de_registro_do_stream_de_limites(registro(imagem(), reservou)) is None


def test_so_versao_legado_mudou_nao_gera_evento() -> None:
    assert de_registro_do_stream_de_limites(registro(imagem(), imagem(versao_legado=2))) is None


@pytest.mark.parametrize("nome", ["INSERT", "REMOVE"])
def test_outros_tipos_de_registro_sao_ignorados(nome: str) -> None:
    assert de_registro_do_stream_de_limites(registro(imagem(), imagem("9.00"), nome)) is None


def test_modify_sem_old_image_e_malformado_nao_some() -> None:
    r = alteracao()
    del r["dynamodb"]["OldImage"]

    with pytest.raises(RegistroMalformado, match="OldImage"):
        de_registro_do_stream_de_limites(r)


def _sem_versao() -> dict[str, Any]:
    depois = imagem("1500.00", versao=2)
    del depois["versao"]
    return registro(imagem(), depois)


@pytest.mark.parametrize(
    "estragar",
    [
        pytest.param(lambda d: d.pop("versao"), id="sem-versao"),
        pytest.param(lambda d: d.pop("concedido"), id="sem-concedido"),
        pytest.param(lambda d: d.update(versao={"S": "x"}), id="versao-nao-numerica"),
    ],
)
def test_imagem_malformada_levanta_em_vez_de_descartar(estragar: Any) -> None:
    depois = imagem("1500.00", versao=2)
    estragar(depois)

    with pytest.raises(RegistroMalformado):
        de_registro_do_stream_de_limites(registro(imagem(), depois))


def test_outbox_noop() -> None:
    assert list(OutboxNoop().paginas_antigas(10**18)) == []
    assert OutboxNoop().apagar(["a"]) is None


class PublicadorFalso:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def publicar(self, lote: list[EventoPendente]) -> set[str]:
        return {e.id_evento for e in lote} if self.ok else set()


@pytest.mark.parametrize(("ok", "falhas"), [(True, []), (False, [{"itemIdentifier": "100"}])])
def test_handler_reporta_falha_por_sequencia(
    monkeypatch: pytest.MonkeyPatch, ok: bool, falhas: list[dict[str, str]]
) -> None:
    from relay import lambda_handler  # noqa: PLC0415

    monkeypatch.setattr(
        lambda_handler,
        "_cdc_limites",
        lambda_handler.ComponentesCdcLimites(PublicadorFalso(ok), OutboxNoop()),  # type: ignore[arg-type]
    )
    reserva = registro(imagem(), imagem(disponivel="900.00", reservado="100.00", versao=2))

    resposta = handler_cdc_limites({"Records": [alteracao(), reserva]}, None)

    assert resposta == {"batchItemFailures": falhas}


def test_handler_devolve_o_malformado_como_falha_e_publica_o_resto(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F-6: o malformado volta ao ESM (retry e, esgotado, DLQ on-failure com alarme); o válido sai."""
    from relay import lambda_handler  # noqa: PLC0415

    publicador = PublicadorFalso(True)
    monkeypatch.setattr(
        lambda_handler,
        "_cdc_limites",
        lambda_handler.ComponentesCdcLimites(publicador, OutboxNoop()),  # type: ignore[arg-type]
    )
    ruim = _sem_versao()
    ruim["dynamodb"]["SequenceNumber"] = "200"

    with caplog.at_level(logging.ERROR, logger="relay.lambda_handler"):
        resposta = handler_cdc_limites({"Records": [alteracao(), ruim]}, None)

    assert resposta == {"batchItemFailures": [{"itemIdentifier": "200"}]}
    linhas = [json.loads(r.getMessage()) for r in caplog.records if r.levelno == logging.ERROR]
    assert [(x["evento"], x["sequencia"]) for x in linhas] == [("cdc_registro_malformado", "200")]
