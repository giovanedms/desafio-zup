"""Composition root do serviço limites: o único lugar que conhece adapters concretos e config.

Execução: `uvicorn limites.main:app_factory --factory`. O cliente boto3 nasce dentro da factory
(não no import do módulo), então importar este módulo nunca abre conexão nem lê credencial.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

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

# Endpoint local por allowlist exata, sem parser de URL: urlparse e o urllib3 do botocore discordam em casos como
# `http://exemplo.com\@localhost/`, que um leria como local e o outro discaria para fora. Porta de 1 a 65535;
# ASCII para o K de Kelvin não casar com `k` no IGNORECASE. A mesma regex em todo guard do repositório
# (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_LOCAL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)


def cliente_aws(servico: str, config: Configuracao, opcoes: Config | None = None) -> Any:
    """Na AWS: credenciais do papel de execução. Com endpoint: SÓ local, credenciais fictícias explícitas
    (o shell do operador exporta credenciais reais; nada aqui confia no ambiente). Todo cliente boto3 do
    serviço nasce aqui, inclusive o do caminho quente, que soma os próprios timeouts em `opcoes` aos padrões:
    retry standard e endpoint só o explícito (nunca `AWS_ENDPOINT_URL_<SERVIÇO>` nem o do `~/.aws/config`)."""
    padrao = Config(retries={"mode": "standard"})
    # A trava entra por último: nem `opcoes` a desliga. Os stubs do botocore não declaram o parâmetro
    # (existe desde o 1.31).
    trava = Config(ignore_configured_endpoint_urls=True)  # type: ignore[call-arg]
    opcoes = (padrao if opcoes is None else padrao.merge(opcoes)).merge(trava)
    if config.endpoint_aws is None:
        return boto3.client(servico, region_name=config.regiao, config=opcoes)  # type: ignore[call-overload]
    if not ENDPOINT_LOCAL.fullmatch(config.endpoint_aws):
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
    cliente = cliente_aws(
        "dynamodb",
        config,
        Config(
            max_pool_connections=64,
            # 3 tentativas NO TOTAL (max_attempts contaria 3 novas, 4 no total): ~12 s por chamada, base da conta
            # do pior caso de escrita (~38 s) contra os 60 s de graça do reconciliador (ADR-0006).
            retries={"mode": "standard", "total_max_attempts": 3},
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
