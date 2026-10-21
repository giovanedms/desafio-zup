"""Composition root: único lugar que conhece os adapters concretos e os liga ao caso de uso."""

from __future__ import annotations

import re
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from contabilizacao.adapters.outbound.dynamodb import RepositorioContabilDynamoDB
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.config import Config
from contabilizacao.domain.regras import LivroDeRegras

# Endpoint local por allowlist exata, sem parser de URL: urlparse e o urllib3 do botocore discordam em casos como
# `http://exemplo.com\@localhost/`, que um leria como local e o outro discaria para fora. Porta de 1 a 65535;
# ASCII para o K de Kelvin não casar com `k` no IGNORECASE. A mesma regex em todo guard do repositório
# (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_LOCAL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)


def cliente_aws(servico: str, config: Config) -> Any:
    """Na AWS: credenciais do papel de execução. Com endpoint: SÓ local, credenciais fictícias explícitas
    (o shell do operador exporta credenciais reais; nada aqui confia no ambiente)."""
    # Endpoint só o explícito: `AWS_ENDPOINT_URL_<SERVIÇO>` ou o `~/.aws/config` não desviam o cliente.
    opcoes = BotoConfig(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
        retries={"mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    if config.endpoint is None:
        return boto3.client(servico, region_name=config.regiao, config=opcoes)  # type: ignore[call-overload]
    if not ENDPOINT_LOCAL.fullmatch(config.endpoint):
        raise ValueError(f"endpoint não é local: {config.endpoint!r}")
    return boto3.client(  # type: ignore[call-overload]
        servico,
        endpoint_url=config.endpoint,
        region_name=config.regiao,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=opcoes,
    )


def construir_caso_de_uso(config: Config) -> ContabilizarEvento:
    repositorio = RepositorioContabilDynamoDB(
        cliente_aws("dynamodb", config), config.tabela_lancamentos, config.tabela_inbox
    )
    return ContabilizarEvento(repositorio, LivroDeRegras())
