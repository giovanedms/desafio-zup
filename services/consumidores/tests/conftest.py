"""Fixtures dos consumidores: catálogo lido de contracts/events, mensagens SQS montadas a partir dos exemplos publicados
e, na integração, uma tabela de inbox própria na LocalStack (nunca a AWS real)."""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.inbox_dynamodb import definicao_da_tabela

# O cliente do relay já traz o guard de endpoint local e as credenciais fictícias explícitas.
from relay.local import cliente_local

RAIZ = next(p for p in Path(__file__).resolve().parents if (p / "contracts" / "events").is_dir())
CONTRATOS = RAIZ / "contracts" / "events"
ENDPOINT_LOCAL = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")

# source do EventBridge por source do CloudEvent (o relay publica o de autorização; o CDC, o de limites).
SOURCE_NO_BUS = {
    "/financas/autorizacao": "financas.autorizacao",
    "/financas/limites": "financas.limites",
}


def _exemplo(nome: str) -> dict[str, Any]:
    """O exemplo publicado em contracts/events/examples (`transacao-autorizada`, `limite-alterado`...)."""
    return dict(json.loads((CONTRATOS / "examples" / f"{nome}.v1.json").read_text()))


@pytest.fixture(scope="session")
def catalogo() -> CatalogoDeEsquemas:
    return CatalogoDeEsquemas.do_diretorio(CONTRATOS)


@pytest.fixture(scope="session")  # fábricas sem estado: servem também aos testes de propriedade
def cloudevent() -> Callable[..., dict[str, Any]]:
    """Fábrica: o exemplo do tipo com `id` novo (data.event_id junto) e os campos de `data` sobrescritos."""

    def fabricar(nome: str = "transacao-autorizada", **data: Any) -> dict[str, Any]:
        ce = copy.deepcopy(_exemplo(nome))
        ce["id"] = ce["data"]["event_id"] = str(uuid.uuid4())
        ce["data"].update(data)
        return ce

    return fabricar


@pytest.fixture(scope="session")
def mensagem() -> Callable[..., str]:
    """Fábrica: corpo SQS como o EventBridge entrega (envelope com `detail-type` e o CloudEvent em `detail`). Cada
    chamada tem `id` de envelope novo, como numa republicação do relay ou do sweeper."""

    def montar(ce: dict[str, Any], detail_type: str | None = None) -> str:
        return json.dumps(
            {
                "version": "0",
                "id": str(uuid.uuid4()),
                "detail-type": detail_type or ce["data"]["event_type"],
                "source": SOURCE_NO_BUS.get(ce["source"], "financas.autorizacao"),
                "account": "123456789012",
                "time": ce["time"],
                "region": "sa-east-1",
                "resources": [],
                "detail": ce,
            }
        )

    return montar


def _cliente_local(servico: str) -> Any:
    return cliente_local(servico, ENDPOINT_LOCAL)


@pytest.fixture(scope="session")
def cliente_dynamodb_local() -> Any:
    return _cliente_local("dynamodb")


@pytest.fixture(scope="session")
def cliente_sqs_local() -> Any:
    return _cliente_local("sqs")


@pytest.fixture(scope="session")
def cliente_events_local() -> Any:
    return _cliente_local("events")


@pytest.fixture
def tabela_inbox(cliente_dynamodb_local: Any) -> Iterator[str]:
    nome = f"consumidor-inbox-teste-{uuid.uuid4().hex[:8]}"
    cliente_dynamodb_local.create_table(**definicao_da_tabela(nome))
    cliente_dynamodb_local.get_waiter("table_exists").wait(TableName=nome)
    yield nome
    cliente_dynamodb_local.delete_table(TableName=nome)
