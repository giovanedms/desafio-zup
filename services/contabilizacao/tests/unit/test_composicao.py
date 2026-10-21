"""Composition root da contabilização: endpoint só local e explícito, nunca o de variável de ambiente."""

from __future__ import annotations

from dataclasses import replace

import pytest
from contabilizacao import main
from contabilizacao.config import Config, do_ambiente


def _config(endpoint: str | None) -> Config:
    return replace(do_ambiente({}), regiao="sa-east-1", endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint", ["https://dynamodb.exemplo.com", "http://exemplo.com\\@localhost/"]
)
def test_endpoint_remoto_e_recusado(endpoint: str) -> None:
    with pytest.raises(ValueError, match="não é local"):
        main.cliente_aws("dynamodb", _config(endpoint))


def test_endpoint_local_e_aceito() -> None:
    cliente = main.cliente_aws("dynamodb", _config("http://localstack:4566"))
    assert cliente.meta.endpoint_url == "http://localstack:4566"


def test_endpoint_de_variavel_de_ambiente_nao_desvia_o_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "https://remoto.exemplo.com")
    cliente = main.cliente_aws("dynamodb", _config(None))
    assert cliente.meta.endpoint_url == "https://dynamodb.sa-east-1.amazonaws.com"
