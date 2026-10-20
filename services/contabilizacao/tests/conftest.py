"""Fixtures compartilhadas. Integração SÓ contra a LocalStack local — nunca contra a AWS real."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import boto3
import pytest
from botocore.config import Config
from contabilizacao.adapters.outbound.dynamodb import definicoes_das_tabelas

ENDPOINT_LOCAL = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")


def _cliente_local(servico: str) -> Any:
    """Endpoint e credenciais EXPLÍCITOS: o shell do operador exporta credenciais reais."""
    host = urlparse(ENDPOINT_LOCAL).hostname
    if host not in {"127.0.0.1", "localhost", "localstack"}:
        pytest.fail(f"endpoint de teste precisa ser local, recebi {ENDPOINT_LOCAL!r}")
    return boto3.client(
        servico,
        endpoint_url=ENDPOINT_LOCAL,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(max_pool_connections=64, retries={"mode": "standard"}),
    )


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
def tabelas_contabilizacao(cliente_dynamodb_local: Any) -> Iterator[tuple[str, str]]:
    sufixo = uuid.uuid4().hex[:8]
    nomes = (f"lancamentos-teste-{sufixo}", f"inbox-teste-{sufixo}")
    for definicao in definicoes_das_tabelas(*nomes):
        cliente_dynamodb_local.create_table(**definicao)
    for nome in nomes:
        cliente_dynamodb_local.get_waiter("table_exists").wait(TableName=nome)
    yield nomes
    for nome in nomes:
        cliente_dynamodb_local.delete_table(TableName=nome)
