"""Loop do poller local: o heartbeat (healthcheck da imagem) só renova em volta sem exceção (F7-2)."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from contabilizacao.adapters.inbound import poller
from contabilizacao.config import do_ambiente


class Parar(BaseException):
    """Escapa do `except Exception` do loop: encerra o `main()` depois de N voltas."""


def _rodar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, falhar: bool, voltas: int = 3) -> Path:
    batida = tmp_path / "contabilizacao.heartbeat"
    batida.touch()
    os.utime(batida, (0, 0))  # heartbeat velho: só uma volta bem-sucedida o renova
    chamadas = 0

    def iteracao(*_: Any) -> Counter[str]:
        nonlocal chamadas
        chamadas += 1
        if chamadas > voltas:
            raise Parar
        if falhar:
            raise RuntimeError("SQS fora do ar")
        return Counter()

    monkeypatch.setattr(poller, "HEARTBEAT", batida)
    monkeypatch.setattr(
        poller, "do_ambiente", lambda: do_ambiente({"AWS_ENDPOINT_URL": "http://127.0.0.1:4566"})
    )
    monkeypatch.setattr(poller, "cliente_aws", lambda *_a, **_k: object())
    monkeypatch.setattr(poller, "construir_caso_de_uso", lambda _c: object())
    monkeypatch.setattr(poller, "_esperar_fila", lambda *_a, **_k: "http://fila")
    monkeypatch.setattr(poller, "uma_iteracao", iteracao)
    monkeypatch.setattr(poller.time, "sleep", lambda _s: None)

    with pytest.raises(Parar):
        poller.main()
    assert chamadas == voltas + 1
    return batida


def test_volta_que_falha_nao_renova_o_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _rodar(monkeypatch, tmp_path, falhar=True).stat().st_mtime == 0


def test_volta_sem_excecao_renova_o_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _rodar(monkeypatch, tmp_path, falhar=False).stat().st_mtime > 0
