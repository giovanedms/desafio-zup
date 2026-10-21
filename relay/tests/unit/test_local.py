"""Modo local: retomada do iterador expirado, registro malformado (CDC e outbox) e heartbeat do loop."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from relay.cdc_limites import de_registro_do_stream_de_limites
from relay.local import LeitorDeStream
from relay.nucleo import ResultadoDoLote

from relay import local


def _erro(codigo: str) -> ClientError:
    return ClientError({"Error": {"Code": codigo, "Message": "x"}}, "GetRecords")


def _insert(seq: str) -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {
                "pk": {"S": f"OUTBOX#ev-{seq}"},
                "id_evento": {"S": f"ev-{seq}"},
                "detail_type": {"S": "TransacaoAutorizada"},
                "cloudevent": {"S": "{}"},
                "criado_em_ms": {"N": "1"},
            },
        },
    }


class StreamsFalso:
    """Um shard; `get_records` devolve as respostas roteirizadas (dict ou exceção), uma por chamada."""

    def __init__(self, roteiro: list[dict[str, Any] | ClientError]) -> None:
        self.roteiro = roteiro
        self.pedidos_de_iterador: list[dict[str, Any]] = []

    def describe_stream(self, **_: Any) -> dict[str, Any]:
        return {"StreamDescription": {"Shards": [{"ShardId": "shard-1"}]}}

    def get_shard_iterator(self, **kw: Any) -> dict[str, str]:
        self.pedidos_de_iterador.append(kw)
        return {"ShardIterator": f"it-{len(self.pedidos_de_iterador)}"}

    def get_records(self, ShardIterator: str) -> dict[str, Any]:  # noqa: N803
        passo = self.roteiro.pop(0)
        if isinstance(passo, ClientError):
            raise passo
        return passo


def _tipos(streams: StreamsFalso) -> list[tuple[str, str | None]]:
    return [(p["ShardIteratorType"], p.get("SequenceNumber")) for p in streams.pedidos_de_iterador]


def test_iterador_expirado_retoma_depois_da_ultima_sequencia_lida() -> None:
    streams = StreamsFalso(
        [
            {"Records": [_insert("101"), _insert("102")], "NextShardIterator": "a"},
            _erro("ExpiredIteratorException"),
            {"Records": [_insert("103")], "NextShardIterator": "b"},
        ]
    )
    leitor = LeitorDeStream(streams, "arn")  # type: ignore[arg-type]

    lidos = [e.id_evento for _ in range(3) for e in leitor.ler()]

    assert lidos == ["ev-101", "ev-102", "ev-103"]  # nada relido do começo do shard
    assert _tipos(streams) == [("TRIM_HORIZON", None), ("AFTER_SEQUENCE_NUMBER", "102")]


def test_iterador_expirado_sem_nada_lido_comeca_do_mais_antigo() -> None:
    streams = StreamsFalso([_erro("ExpiredIteratorException"), {"Records": []}])
    leitor = LeitorDeStream(streams, "arn")  # type: ignore[arg-type]

    leitor.ler()

    assert _tipos(streams) == [("TRIM_HORIZON", None), ("TRIM_HORIZON", None)]


def test_dados_aparados_recomecam_do_mais_antigo_disponivel() -> None:
    streams = StreamsFalso(
        [
            {"Records": [_insert("101")], "NextShardIterator": "a"},
            _erro("TrimmedDataAccessException"),
        ]
    )
    leitor = LeitorDeStream(streams, "arn")  # type: ignore[arg-type]

    leitor.ler()
    leitor.ler()

    assert _tipos(streams) == [("TRIM_HORIZON", None), ("TRIM_HORIZON", None)]


def _modify(seq: str, versao: dict[str, str] | None) -> dict[str, Any]:
    def imagem(concedido: str) -> dict[str, Any]:
        img = {
            "id_contrato": {"S": "CTR-1"},
            "concedido": {"N": concedido},
            "status": {"S": "ATIVO"},
            "disponivel": {"N": concedido},
            "versao_legado": {"N": "2"},
        }
        if versao is not None:
            img["versao"] = versao
        return img

    return {
        "eventName": "MODIFY",
        "dynamodb": {
            "SequenceNumber": seq,
            "OldImage": imagem("1000.00"),
            "NewImage": imagem("1500.00"),
        },
    }


def test_cdc_malformado_e_logado_e_o_resto_do_shard_segue(caplog: pytest.LogCaptureFixture) -> None:
    streams = StreamsFalso(
        [
            {
                "Records": [_modify("201", None), _modify("202", {"N": "7"})],
                "NextShardIterator": "a",
            }
        ]
    )
    leitor = LeitorDeStream(streams, "arn", de_registro_do_stream_de_limites)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="relay.local"):
        pendentes = leitor.ler()

    assert len(pendentes) == 1  # o 202 virou LimiteAlterado
    linha = json.loads(caplog.records[-1].getMessage())
    assert (linha["evento"], linha["shard"], linha["sequencia"]) == (
        "cdc_registro_malformado",
        "shard-1",
        "201",
    )


def test_outbox_malformado_e_logado_com_o_pk_e_o_resto_do_shard_segue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sem DLQ no modo local: loga (mesmo termo do metric filter da AWS) e segue; o sweeper o reencontra."""
    ruim = _insert("301")
    del ruim["dynamodb"]["NewImage"]["detail_type"]
    streams = StreamsFalso([{"Records": [ruim, _insert("302")], "NextShardIterator": "a"}])
    leitor = LeitorDeStream(streams, "arn")  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="relay.local"):
        pendentes = leitor.ler()

    assert [e.id_evento for e in pendentes] == ["ev-302"]
    linha = json.loads(caplog.records[-1].getMessage())
    assert (linha["evento"], linha["pk"], linha["shard"], linha["sequencia"]) == (
        "outbox_registro_malformado",
        "OUTBOX#ev-301",
        "shard-1",
        "301",
    )


class Parar(BaseException):
    """Escapa do `except Exception` do loop: encerra o `main()` depois de N voltas."""


def _rodar_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, falhar: bool, voltas: int = 3
) -> Path:
    batida = tmp_path / "relay.heartbeat"
    batida.touch()
    os.utime(batida, (0, 0))  # heartbeat velho: só uma volta bem-sucedida o renova
    chamadas = 0

    def iteracao(*_: Any) -> ResultadoDoLote:
        nonlocal chamadas
        chamadas += 1
        if chamadas > voltas:
            raise Parar
        if falhar:
            raise RuntimeError("LocalStack fora do ar")
        return ResultadoDoLote(set(), set())

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
    monkeypatch.setattr(local, "HEARTBEAT", batida)
    monkeypatch.setattr(local, "cliente_local", lambda *_a, **_k: object())
    monkeypatch.setattr(local, "_descobrir_stream_arn", lambda *_a: "arn")
    monkeypatch.setattr(local, "_leitor_de_limites", lambda *_a: None)
    monkeypatch.setattr(local, "uma_iteracao", iteracao)
    monkeypatch.setattr(local.time, "sleep", lambda _s: None)

    with pytest.raises(Parar):
        local.main()
    assert chamadas == voltas + 1
    return batida


def test_volta_que_falha_nao_renova_o_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F7-2: com toda volta falhando, o healthcheck da imagem (heartbeat < 30 s) reprova o relay."""
    assert _rodar_main(monkeypatch, tmp_path, falhar=True).stat().st_mtime == 0


def test_volta_sem_excecao_renova_o_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _rodar_main(monkeypatch, tmp_path, falhar=False).stat().st_mtime > 0
