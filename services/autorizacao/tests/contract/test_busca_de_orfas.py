"""Suíte de contrato da porta BuscaDeOrfas — a MESMA suíte nos dois adapters (LSP).

No DynamoDB a busca é uma Query nos 10 shards do GSI esparso `gsi_orfas`; o GSI é eventualmente
consistente, então as asserções sobre o índice esperam a convergência (`ate_convergir`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import pytest
from autorizacao.adapters.outbound.dynamodb import RepositorioDeAutorizacoesDynamoDB
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.portas import BuscaDeOrfas, RepositorioDeAutorizacoes
from autorizacao.domain.autorizacao import DURACAO_DO_LEASE, Autorizacao
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

    class Repositorio(RepositorioDeAutorizacoes, BuscaDeOrfas, Protocol):
        """O que a suíte exercita: gravar/assumir/concluir + listar órfãs."""


AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
VENCIDO = (
    AGORA + DURACAO_DO_LEASE + timedelta(seconds=1)
)  # instante em que o lease de `nova` venceu
SALDOS = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 2
)


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def repositorio(request: pytest.FixtureRequest) -> Repositorio:
    if request.param == "memoria":
        return RepositorioDeAutorizacoesEmMemoria()
    cliente: DynamoDBClient = request.getfixturevalue("cliente_dynamodb_local")
    tabela, outbox = request.getfixturevalue("tabelas_autorizacoes")
    return RepositorioDeAutorizacoesDynamoDB(cliente, tabela, outbox)


def nova(id_autorizacao: str, criada_em: datetime = AGORA) -> Autorizacao:
    return Autorizacao.iniciar(
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("150.00"), "BRL", TipoOperacao.RESERVA),
        id_autorizacao=id_autorizacao,
        impressao_digital="imp",
        client_id="canal-pos",
        agora=criada_em,
        correlation_id="corr-1",
        traceparent=None,
    )


def ids(orfas: list[Autorizacao]) -> list[str]:
    return [a.id_autorizacao for a in orfas]


def ate_convergir(
    consulta: Callable[[], list[str]], esperado: list[str], tentativas: int = 40
) -> list[str]:
    """GSI é eventualmente consistente: repete a consulta até bater (ou devolve o último resultado)."""
    resultado = consulta()
    for _ in range(tentativas):
        if resultado == esperado:
            break
        time.sleep(0.25)
        resultado = consulta()
    return resultado


def test_so_em_processamento_com_lease_vencido_aparece(repositorio: Repositorio) -> None:
    repositorio.registrar(nova("vencida"))
    repositorio.registrar(nova("viva", AGORA + timedelta(minutes=5)))
    concluida = nova("concluida")
    repositorio.registrar(concluida)
    repositorio.concluir(concluida.aprovar(SALDOS, AGORA, "ev-1"), None)

    achadas = ate_convergir(lambda: ids(repositorio.listar_orfas(VENCIDO, 100)), ["vencida"])

    assert achadas == ["vencida"]


def test_lease_ainda_valido_nao_e_orfa(repositorio: Repositorio) -> None:
    repositorio.registrar(nova("a"))

    assert (
        repositorio.listar_orfas(AGORA + DURACAO_DO_LEASE, 100) == []
    )  # vence só DEPOIS do instante


def test_concluida_sai_da_lista(repositorio: Repositorio) -> None:
    a = nova("a")
    repositorio.registrar(a)
    assert ate_convergir(lambda: ids(repositorio.listar_orfas(VENCIDO, 100)), ["a"]) == ["a"]

    repositorio.concluir(a.aprovar(SALDOS, AGORA, "ev-1"), None)

    assert ate_convergir(lambda: ids(repositorio.listar_orfas(VENCIDO, 100)), []) == []


def test_assumida_sai_ate_o_novo_lease_vencer(repositorio: Repositorio) -> None:
    a = nova("a")
    repositorio.registrar(a)
    assert ate_convergir(lambda: ids(repositorio.listar_orfas(VENCIDO, 100)), ["a"]) == ["a"]
    assumida = a.assumir(VENCIDO)  # novo lease: VENCIDO + 10 s
    assert repositorio.assumir(assumida, a.lease_ate)

    durante = ate_convergir(lambda: ids(repositorio.listar_orfas(VENCIDO, 100)), [])
    de_novo = ate_convergir(
        lambda: ids(repositorio.listar_orfas(assumida.lease_ate + timedelta(seconds=1), 100)), ["a"]
    )

    assert durante == []
    assert de_novo == ["a"]


def test_ordem_por_lease_e_corte_no_limite(repositorio: Repositorio) -> None:
    for i, deslocamento in enumerate([30, 10, 20, 40]):  # criadas fora de ordem
        repositorio.registrar(nova(f"aut-{i}", AGORA + timedelta(seconds=deslocamento)))
    depois_de_todas = AGORA + timedelta(minutes=5)
    esperado = ["aut-1", "aut-2", "aut-0"]  # lease mais antigo primeiro, cortado em 3

    achadas = ate_convergir(lambda: ids(repositorio.listar_orfas(depois_de_todas, 3)), esperado)

    assert achadas == esperado
