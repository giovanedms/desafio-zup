"""Fixtures compartilhadas. Integração SÓ contra a LocalStack local — nunca contra a AWS real."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import boto3
import pytest
from autorizacao.adapters.outbound.dynamodb import definicoes_das_tabelas
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

ENDPOINT_LOCAL = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")

# Mesma allowlist dos composition roots, sem parser de URL (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_PERMITIDO = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)


@pytest.fixture(scope="session")
def cliente_dynamodb_local() -> DynamoDBClient:
    """Endpoint e credenciais EXPLÍCITOS: o shell do operador exporta credenciais reais."""
    if not ENDPOINT_PERMITIDO.fullmatch(ENDPOINT_LOCAL):
        pytest.fail(f"endpoint de teste precisa ser local, recebi {ENDPOINT_LOCAL!r}")
    return boto3.client(
        "dynamodb",
        endpoint_url=ENDPOINT_LOCAL,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(
            max_pool_connections=64,
            retries={"mode": "standard"},
            ignore_configured_endpoint_urls=True,  # endpoint só o explícito, como nos guards
        ),
    )


@pytest.fixture
def tabelas_autorizacoes(cliente_dynamodb_local: DynamoDBClient) -> Iterator[tuple[str, str]]:
    sufixo = uuid.uuid4().hex[:8]
    nomes = (f"autorizacoes-teste-{sufixo}", f"autorizacoes-outbox-teste-{sufixo}")
    for definicao in definicoes_das_tabelas(*nomes):
        cliente_dynamodb_local.create_table(**definicao)
    for nome in nomes:
        cliente_dynamodb_local.get_waiter("table_exists").wait(TableName=nome)
    yield nomes
    for nome in nomes:
        cliente_dynamodb_local.delete_table(TableName=nome)
