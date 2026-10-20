"""Composition root do serviço limites: o único lugar que conhece adapters concretos e config.

Execução: `uvicorn limites.main:app_factory --factory`. O cliente boto3 nasce dentro da factory
(não no import do módulo), então importar este módulo nunca abre conexão nem lê credencial.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from fastapi import FastAPI

from limites.adapters.inbound.http import criar_app
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB
from limites.application.casos_de_uso import (
    AplicarOperacaoNoLimite,
    ConsultarLimite,
    SincronizarComLegado,
)
from limites.config import Configuracao

HOSTS_LOCAIS = {"127.0.0.1", "localhost", "localstack"}


def cliente_aws(servico: str, config: Configuracao) -> Any:
    """Na AWS: credenciais do papel de execução. Com endpoint: SÓ local, credenciais fictícias explícitas
    (o shell do operador exporta credenciais reais; nada aqui confia no ambiente)."""
    opcoes = Config(retries={"mode": "standard"})
    if config.endpoint_aws is None:
        return boto3.client(servico, region_name=config.regiao, config=opcoes)  # type: ignore[call-overload]
    if urlparse(config.endpoint_aws).hostname not in HOSTS_LOCAIS:
        raise ValueError(f"endpoint não é local: {config.endpoint_aws!r}")
    return boto3.client(  # type: ignore[call-overload]
        servico,
        endpoint_url=config.endpoint_aws,
        region_name=config.regiao,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=opcoes,
    )


class RelogioDoSistema:
    def agora(self) -> datetime:
        return datetime.now(UTC)


def configurar_log_da_guarda() -> None:
    """Linha JSON pura no stdout (sem prefixo): o metric filter e o Contributor Insights do CloudWatch
    só leem campos de eventos que sejam JSON do primeiro ao último caractere."""
    log = logging.getLogger("limites.guarda")
    if log.handlers:
        return  # idempotente: a factory pode ser chamada mais de uma vez no mesmo processo
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(saida)
    log.setLevel(logging.WARNING)
    log.propagate = False


def construir_app(config: Configuracao) -> FastAPI:
    configurar_log_da_guarda()
    cliente = boto3.client(
        "dynamodb",
        region_name=config.regiao,
        endpoint_url=config.endpoint_aws,
        config=Config(
            max_pool_connections=64,
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=1,
            read_timeout=2,
        ),
    )
    repositorio = RepositorioDeLimitesDynamoDB(cliente, config.tabela_limites)
    return criar_app(
        AplicarOperacaoNoLimite(repositorio, RelogioDoSistema()), ConsultarLimite(repositorio)
    )


def construir_sincronizacao(config: Configuracao) -> SincronizarComLegado:
    """Composition da ACL do legado (Lambda SQS e loop local): mesmo repositório do caminho quente."""
    repositorio = RepositorioDeLimitesDynamoDB(
        cliente_aws("dynamodb", config), config.tabela_limites
    )
    return SincronizarComLegado(repositorio)


def app_factory() -> FastAPI:
    """Entrada do uvicorn (`--factory`): configuração lida do ambiente a cada subida."""
    return construir_app(Configuracao.do_ambiente())
