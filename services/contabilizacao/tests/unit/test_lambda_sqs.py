"""Handler da Lambda: sucesso, duplicata e evento inválido → batchItemFailures com o messageId certo."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from contabilizacao.adapters.inbound import lambda_sqs
from contabilizacao.adapters.outbound.memoria import RepositorioContabilEmMemoria
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.domain.regras import LivroDeRegras


def corpo(id_evento: str | None = None, tipo: str = "DEBITO") -> str:
    ce = {
        "id": id_evento or str(uuid.uuid4()),
        "source": "/financas/autorizacao",
        "time": "2026-10-21T02:30:00.000Z",
        "data": {
            "event_type": "TransacaoAutorizada",
            "id_contrato": "CTR-1",
            "valor": 150.25,
            "tipo_operacao": tipo,
        },
    }
    return json.dumps({"version": "0", "detail-type": "TransacaoAutorizada", "detail": ce})


def registro(message_id: str, body: str) -> dict[str, Any]:
    return {"messageId": message_id, "body": body}


@pytest.fixture
def repositorio(monkeypatch: pytest.MonkeyPatch) -> RepositorioContabilEmMemoria:
    repo = RepositorioContabilEmMemoria()
    monkeypatch.setattr(lambda_sqs, "_caso_de_uso", ContabilizarEvento(repo, LivroDeRegras()))
    return repo


def test_sucesso_nao_reporta_falhas(repositorio: RepositorioContabilEmMemoria) -> None:
    resposta = lambda_sqs.handler({"Records": [registro("m1", corpo())]}, None)

    assert resposta == {"batchItemFailures": []}
    assert len(repositorio.lancamentos) == 1


def test_duplicata_e_sucesso_sem_segundo_lancamento(
    repositorio: RepositorioContabilEmMemoria,
) -> None:
    body = corpo("evt-1")
    resposta = lambda_sqs.handler({"Records": [registro("m1", body), registro("m2", body)]}, None)

    assert resposta == {"batchItemFailures": []}
    assert len(repositorio.lancamentos) == 1


def test_evento_invalido_reporta_so_o_message_id_dele(
    repositorio: RepositorioContabilEmMemoria,
) -> None:
    lote = {
        "Records": [
            registro("ok-1", corpo()),
            registro("ruim-json", "isto não é json"),
            registro("ruim-regra", corpo(tipo="INEXISTENTE")),
            registro("ok-2", corpo()),
        ]
    }
    resposta = lambda_sqs.handler(lote, None)

    assert resposta == {
        "batchItemFailures": [{"itemIdentifier": "ruim-json"}, {"itemIdentifier": "ruim-regra"}]
    }
    assert len(repositorio.lancamentos) == 2
