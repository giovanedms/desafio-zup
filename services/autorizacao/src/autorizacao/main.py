"""Composition root do serviço autorizacao: o único lugar que conhece adapters concretos e config.

Execução: `uvicorn autorizacao.main:app_factory --factory`. Clientes (boto3, httpx) nascem dentro da
factory, então importar este módulo nunca abre conexão nem lê credencial.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

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


def _construir_nucleo(config: Configuracao) -> Nucleo:
    dynamodb = boto3.client(
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
    http_limites = httpx.Client(
        base_url=config.url_limites,
        timeout=httpx.Timeout(connect=0.5, read=1.5, write=0.5, pool=0.5),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    repositorio = RepositorioDeAutorizacoesDynamoDB(
        dynamodb, config.tabela_autorizacoes, config.tabela_outbox
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
