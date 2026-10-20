"""Fixtures compartilhadas. Integração SÓ contra a LocalStack local — nunca contra a AWS real."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import boto3
import pytest
from botocore.config import Config
from limites.adapters.outbound.dynamodb import definicao_da_tabela

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

ENDPOINT_LOCAL = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")


@pytest.fixture(scope="session")
def cliente_dynamodb_local() -> DynamoDBClient:
    """Cliente com endpoint e credenciais EXPLÍCITOS.

    O shell do operador exporta credenciais reais em AWS_ACCESS_KEY_ID; por isso não confiamos no
    ambiente: endpoint obrigatoriamente local e credenciais fictícias fixas.
    """
    host = urlparse(ENDPOINT_LOCAL).hostname
    if host not in {"127.0.0.1", "localhost", "localstack"}:
        pytest.fail(f"endpoint de teste precisa ser local, recebi {ENDPOINT_LOCAL!r}")
    return boto3.client(
        "dynamodb",
        endpoint_url=ENDPOINT_LOCAL,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(max_pool_connections=64, retries={"mode": "standard"}),
    )


@pytest.fixture
def tabela_limites(cliente_dynamodb_local: DynamoDBClient) -> Iterator[str]:
    nome = f"limites-teste-{uuid.uuid4().hex[:8]}"
    cliente_dynamodb_local.create_table(**definicao_da_tabela(nome))
    cliente_dynamodb_local.get_waiter("table_exists").wait(TableName=nome)
    yield nome
    cliente_dynamodb_local.delete_table(TableName=nome)
