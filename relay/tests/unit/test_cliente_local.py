"""Relay local: cliente só com endpoint local, comparado por allowlist exata (sem parser de URL)."""

from __future__ import annotations

import pytest
from relay.local import cliente_local


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://dynamodb.exemplo.com",
        "http://localhost.exemplo.com:4566",
        "http://exemplo.com\\@localhost/",  # urlparse lê localhost; o urllib3 do botocore discaria exemplo.com
    ],
)
def test_endpoint_remoto_e_recusado(endpoint: str) -> None:
    with pytest.raises(ValueError, match="não é local"):
        cliente_local("dynamodb", endpoint)


@pytest.mark.parametrize("endpoint", ["http://localstack:4566", "http://127.0.0.1:4566"])
def test_endpoint_local_e_aceito(endpoint: str) -> None:
    cliente = cliente_local("dynamodb", endpoint)
    assert cliente.meta.endpoint_url == endpoint
