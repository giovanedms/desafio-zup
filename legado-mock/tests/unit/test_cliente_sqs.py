"""Mock do legado: fila só na LocalStack, com endpoint explícito e credencial fictícia."""

from __future__ import annotations

import pytest
from legado_mock.__main__ import cliente_sqs


@pytest.mark.parametrize(
    "endpoint", ["https://sqs.exemplo.com", "http://exemplo.com\\@localhost/", "http://localhost:0"]
)
def test_endpoint_remoto_encerra(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
    with pytest.raises(SystemExit, match="não é local"):
        cliente_sqs()


def test_endpoint_local_e_aceito_e_o_explicito_vence_a_variavel_por_servico(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("AWS_ENDPOINT_URL_SQS", "https://remoto.exemplo.com")
    cliente = cliente_sqs()
    assert cliente.meta.endpoint_url == "http://localstack:4566"
