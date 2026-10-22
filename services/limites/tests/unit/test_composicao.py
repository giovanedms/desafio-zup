"""Composition root: todo cliente boto3 do serviço, inclusive o do caminho quente, passa pelo guard de endpoint."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.config import Config
from limites import main
from limites.config import Configuracao

CREDENCIAL_DA_LOCALSTACK = "test"  # a fictícia, que nunca sai da máquina


@pytest.fixture
def chamadas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Troca o boto3.client por um que só registra os argumentos (nenhum cliente real nasce)."""
    registro: list[dict[str, Any]] = []

    def falso(servico: str, **kwargs: Any) -> MagicMock:
        registro.append({"servico": servico, **kwargs})
        return MagicMock()

    monkeypatch.setattr(main.boto3, "client", falso)
    return registro


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://dynamodb.exemplo.com",
        "http://localstack.exemplo.com:4566",
        "http://exemplo.com\\@localhost/",  # urlparse lê localhost; o urllib3 do botocore discaria exemplo.com
    ],
)
def test_app_com_endpoint_remoto_nao_sobe(chamadas: list[dict[str, Any]], endpoint: str) -> None:
    with pytest.raises(ValueError, match="não é local"):
        main.construir_app(Configuracao("limites", "sa-east-1", endpoint))
    assert chamadas == []


def test_app_com_endpoint_local_usa_credencial_ficticia_e_os_timeouts_do_caminho_quente(
    chamadas: list[dict[str, Any]],
) -> None:
    main.construir_app(Configuracao("limites", "sa-east-1", "http://localstack:4566"))
    (chamada,) = chamadas
    assert chamada["endpoint_url"] == "http://localstack:4566"
    assert {chamada["aws_access_key_id"], chamada["aws_secret_access_key"]} == {
        CREDENCIAL_DA_LOCALSTACK
    }
    assert chamada["config"].connect_timeout == 1
    assert chamada["config"].read_timeout == 2


def test_app_sem_endpoint_vale_o_papel_da_task(chamadas: list[dict[str, Any]]) -> None:
    main.construir_app(Configuracao("limites", "sa-east-1", None))
    (chamada,) = chamadas
    assert "endpoint_url" not in chamada
    assert "aws_access_key_id" not in chamada
    assert chamada["config"].max_pool_connections == 64


def test_caminho_quente_faz_3_tentativas_no_total(chamadas: list[dict[str, Any]]) -> None:
    """A conta do pior caso de escrita (~38 s < 60 s de graça, ADR-0006) supõe 3 tentativas NO TOTAL.

    `max_attempts: 3` seriam 3 NOVAS, 4 no total (~19 s por chamada, ~59 s no pior caso).
    """
    main.construir_app(Configuracao("limites", "sa-east-1", None))
    (chamada,) = chamadas
    assert chamada["config"].retries == {"mode": "standard", "total_max_attempts": 3}


def test_opcoes_somam_aos_padroes_sem_perder_retry_nem_a_trava_de_endpoint(
    chamadas: list[dict[str, Any]],
) -> None:
    main.cliente_aws("sqs", Configuracao("limites", "sa-east-1", None), Config(read_timeout=5))
    (chamada,) = chamadas
    assert chamada["config"].read_timeout == 5
    assert chamada["config"].retries == {"mode": "standard"}
    assert chamada["config"].ignore_configured_endpoint_urls is True


def test_opcoes_nao_desligam_a_trava_de_endpoint(chamadas: list[dict[str, Any]]) -> None:
    main.cliente_aws(
        "sqs",
        Configuracao("limites", "sa-east-1", None),
        Config(ignore_configured_endpoint_urls=False),  # type: ignore[call-arg]
    )
    (chamada,) = chamadas
    assert chamada["config"].ignore_configured_endpoint_urls is True


def test_endpoint_de_variavel_de_ambiente_nao_desvia_o_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O botocore lê AWS_ENDPOINT_URL_DYNAMODB sozinho; sem endpoint na configuração, o guard nem roda."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "https://remoto.exemplo.com")

    cliente = main.cliente_aws("dynamodb", Configuracao("limites", "sa-east-1", None))

    assert cliente.meta.endpoint_url == "https://dynamodb.sa-east-1.amazonaws.com"
