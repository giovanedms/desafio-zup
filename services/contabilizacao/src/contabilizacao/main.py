"""Composition root: único lugar que conhece os adapters concretos e os liga ao caso de uso."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig

from contabilizacao.adapters.outbound.dynamodb import RepositorioContabilDynamoDB
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.config import Config
from contabilizacao.domain.regras import LivroDeRegras

HOSTS_LOCAIS = {"127.0.0.1", "localhost", "localstack"}


def cliente_aws(servico: str, config: Config) -> Any:
    """Na AWS: credenciais do papel de execução. Com endpoint: SÓ local, credenciais fictícias explícitas
    (o shell do operador exporta credenciais reais; nada aqui confia no ambiente)."""
    opcoes = BotoConfig(retries={"mode": "standard"})
    if config.endpoint is None:
        return boto3.client(servico, region_name=config.regiao, config=opcoes)  # type: ignore[call-overload]
    if urlparse(config.endpoint).hostname not in HOSTS_LOCAIS:
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
