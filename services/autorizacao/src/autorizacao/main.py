"""Composition root do serviço autorizacao: o único lugar que conhece adapters concretos e config.

Execução: `uvicorn autorizacao.main:app_factory --factory`. Clientes (boto3, httpx) nascem dentro da
factory, então importar este módulo nunca abre conexão nem lê credencial.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
from botocore.config import Config
from fastapi import FastAPI

from autorizacao.adapters.inbound.http import criar_app
from autorizacao.adapters.outbound.disjuntor import Disjuntor
from autorizacao.adapters.outbound.dynamodb import RepositorioDeAutorizacoesDynamoDB
from autorizacao.adapters.outbound.limites_http import GatewayDeLimitesHttp
from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.consultar import ConsultarAutorizacao
from autorizacao.application.expirar import ExpirarReservas
from autorizacao.application.reconciliar import ReconciliarOrfas
from autorizacao.config import Configuracao


class RelogioDoSistema:
    def agora(self) -> datetime:
        return datetime.now(UTC)


class GeradorDeIdsUuid:
    def novo_id(self) -> str:
        return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Nucleo:
    """Componentes compartilhados pelo app HTTP e pelo reconciliador (mesmos adapters, mesmas regras)."""

    repositorio: RepositorioDeAutorizacoesDynamoDB
    limites: GatewayDeLimitesHttp
    autorizar: AutorizarTransacao


# Endpoint local por allowlist exata, sem parser de URL: urlparse e o urllib3 do botocore discordam em casos como
# `http://exemplo.com\@localhost/`, que um leria como local e o outro discaria para fora. Porta de 1 a 65535;
# ASCII para o K de Kelvin não casar com `k` no IGNORECASE. A mesma regex em todo guard do repositório
# (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_LOCAL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)


def cliente_dynamodb(config: Configuracao) -> Any:
    """Na AWS: credenciais do papel da task. Com endpoint: SÓ local, credenciais fictícias explícitas
    (o shell de quem desenvolve pode exportar credenciais reais; o cliente não as leva a um endpoint remoto)."""
    opcoes = Config(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
        max_pool_connections=64,
        retries={"mode": "standard", "max_attempts": 3},
        connect_timeout=1,
        read_timeout=2,
        # Endpoint só o explícito: `AWS_ENDPOINT_URL_DYNAMODB` ou o `~/.aws/config` não desviam o cliente.
        ignore_configured_endpoint_urls=True,
    )
    if config.endpoint_aws is None:
        return boto3.client("dynamodb", region_name=config.regiao, config=opcoes)
    if not ENDPOINT_LOCAL.fullmatch(config.endpoint_aws):
        raise ValueError(f"endpoint não é local: {config.endpoint_aws!r}")
    return boto3.client(
        "dynamodb",
        endpoint_url=config.endpoint_aws,
        region_name=config.regiao,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=opcoes,
    )


def _construir_nucleo(config: Configuracao) -> Nucleo:
    dynamodb = cliente_dynamodb(config)
    http_limites = httpx.Client(
        base_url=config.url_limites,
        timeout=httpx.Timeout(connect=0.5, read=1.5, write=0.5, pool=0.5),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    repositorio = RepositorioDeAutorizacoesDynamoDB(
        dynamodb,
        config.tabela_autorizacoes,
        config.tabela_outbox,
        shards_de_orfas=config.shards_de_orfas,
        shards_de_vencimentos=config.shards_de_vencimentos,
    )
    limites = GatewayDeLimitesHttp(http_limites, Disjuntor())
    autorizar = AutorizarTransacao(repositorio, limites, RelogioDoSistema(), GeradorDeIdsUuid())
    return Nucleo(repositorio, limites, autorizar)


def construir_app(config: Configuracao) -> FastAPI:
    nucleo = _construir_nucleo(config)
    return criar_app(nucleo.autorizar, ConsultarAutorizacao(nucleo.repositorio), config.max_em_voo)


def construir_reconciliador(config: Configuracao) -> tuple[ReconciliarOrfas, ExpirarReservas]:
    """Reconciliador de órfãs (ADR-0006) e expirador de reservas, com os MESMOS componentes do app:
    repositório, gateway do limite (com disjuntor) e o caso de uso que conclui o roll forward."""
    nucleo = _construir_nucleo(config)
    relogio = RelogioDoSistema()
    return (
        ReconciliarOrfas(nucleo.repositorio, nucleo.limites, relogio, nucleo.autorizar),
        ExpirarReservas(nucleo.repositorio, nucleo.limites, relogio, GeradorDeIdsUuid()),
    )


def app_factory() -> FastAPI:
    """Entrada do uvicorn (`--factory`): configuração lida do ambiente a cada subida."""
    return construir_app(Configuracao.do_ambiente())
