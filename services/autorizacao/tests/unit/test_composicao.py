"""Composition root: o cliente do DynamoDB só leva credencial fictícia a endpoint local, nunca a um remoto."""

from __future__ import annotations

from typing import Any

import pytest
from autorizacao import main
from autorizacao.config import Configuracao

CREDENCIAL_DA_LOCALSTACK = "test"  # a fictícia, que nunca sai da máquina
REMOTOS = [
    "https://dynamodb.exemplo.com",
    "http://localhost.exemplo.com:4566",
    "http://127.0.0.1@exemplo.com:4566",
    "http://exemplo.com\\@localhost/",  # urlparse lê localhost; o urllib3 do botocore discaria exemplo.com
    "http://localhost:0",
    "http://localhost:65536",
    "http://localstac\u212a:4566",  # K de Kelvin: casaria com `k` no IGNORECASE sem re.ASCII
]


def _config(endpoint: str | None) -> Configuracao:
    return Configuracao(
        tabela_autorizacoes="autorizacoes",
        tabela_outbox="autorizacoes-outbox",
        regiao="sa-east-1",
        endpoint_aws=endpoint,
        url_limites="http://limites:8080",
        max_em_voo=18,
    )


@pytest.fixture
def chamadas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Troca o boto3.client por um que só registra os argumentos (nenhum cliente real nasce)."""
    registro: list[dict[str, Any]] = []

    def falso(servico: str, **kwargs: Any) -> object:
        registro.append({"servico": servico, **kwargs})
        return object()

    monkeypatch.setattr(main.boto3, "client", falso)
    return registro


@pytest.mark.parametrize("endpoint", REMOTOS)
def test_endpoint_remoto_e_recusado_antes_de_criar_o_cliente(
    chamadas: list[dict[str, Any]], endpoint: str
) -> None:
    with pytest.raises(ValueError, match="não é local"):
        main.cliente_dynamodb(_config(endpoint))
    assert chamadas == []


@pytest.mark.parametrize("endpoint", REMOTOS[:2])
def test_app_e_reconciliador_nao_sobem_com_endpoint_remoto(
    chamadas: list[dict[str, Any]], endpoint: str
) -> None:
    with pytest.raises(ValueError, match="não é local"):
        main.construir_app(_config(endpoint))
    with pytest.raises(ValueError, match="não é local"):
        main.construir_reconciliador(_config(endpoint))
    assert chamadas == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localstack:4566",
        "http://127.0.0.1:4566",
        "http://localhost:4566",
        "HTTP://LOCALHOST:65535",
    ],
)
def test_endpoint_local_usa_credencial_ficticia_explicita(
    chamadas: list[dict[str, Any]], endpoint: str
) -> None:
    main.cliente_dynamodb(_config(endpoint))
    (chamada,) = chamadas
    assert chamada["endpoint_url"] == endpoint
    assert {chamada["aws_access_key_id"], chamada["aws_secret_access_key"]} == {
        CREDENCIAL_DA_LOCALSTACK
    }


def test_sem_endpoint_vale_o_papel_da_task_com_os_timeouts_do_caminho_quente(
    chamadas: list[dict[str, Any]],
) -> None:
    main.cliente_dynamodb(_config(None))
    (chamada,) = chamadas
    assert "endpoint_url" not in chamada
    assert "aws_access_key_id" not in chamada
    assert chamada["config"].connect_timeout == 1
    assert chamada["config"].read_timeout == 2
    assert chamada["config"].max_pool_connections == 64


def test_endpoint_de_variavel_de_ambiente_nao_desvia_o_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O botocore lê AWS_ENDPOINT_URL_DYNAMODB sozinho; sem endpoint na configuração, o guard nem roda."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "https://remoto.exemplo.com")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://remoto.exemplo.com")

    cliente = main.cliente_dynamodb(_config(None))  # cliente real; criar não abre conexão

    assert cliente.meta.endpoint_url == "https://dynamodb.sa-east-1.amazonaws.com"
