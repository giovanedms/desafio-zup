"""Suíte de contrato da porta RepositorioDeAutorizacoes — a MESMA suíte nos dois adapters (LSP)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest
from autorizacao.adapters.outbound.dynamodb import RepositorioDeAutorizacoesDynamoDB
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.portas import (
    LeasePerdido,
    OriginalJaEncerrada,
    RegistroDeCompensacao,
    RepositorioDeAutorizacoes,
)
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    Autorizacao,
    MotivoRecusa,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
SALDOS = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 2
)


@dataclass
class Ambiente:
    repositorio: RepositorioDeAutorizacoes
    eventos_no_outbox: Callable[[], int]


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def ambiente(request: pytest.FixtureRequest) -> Ambiente:
    if request.param == "memoria":
        memoria = RepositorioDeAutorizacoesEmMemoria()
        return Ambiente(memoria, lambda: len(memoria.outbox))
    cliente: DynamoDBClient = request.getfixturevalue("cliente_dynamodb_local")
    tabela, outbox = request.getfixturevalue("tabelas_autorizacoes")
    return Ambiente(
        RepositorioDeAutorizacoesDynamoDB(cliente, tabela, outbox),
        lambda: cliente.scan(TableName=outbox, Select="COUNT")["Count"],
    )


def nova(
    id_autorizacao: str = "aut-1", tipo: TipoOperacao = TipoOperacao.RESERVA, **extra: str
) -> Autorizacao:
    pedido = PedidoDeAutorizacao(
        "CTR-1",
        "0001-1",
        Decimal("150.00"),
        "BRL",
        tipo,
        metadata={"canal": "POS"},
        **extra,  # type: ignore[arg-type]
    )
    return Autorizacao.iniciar(
        pedido,
        id_autorizacao=id_autorizacao,
        impressao_digital="imp",
        client_id="canal-pos",
        agora=AGORA,
        correlation_id="corr-1",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )


def test_registrar_novo_e_depois_devolve_o_existente(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    assert r.registrar(nova()) is None

    existente = r.registrar(nova())

    assert existente is not None
    assert existente.status is StatusAutorizacao.EM_PROCESSAMENTO
    assert existente.metadata == {"canal": "POS"}


def test_obter_reconstroi_o_agregado_fielmente(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    original = nova()
    r.registrar(original)
    aprovada = original.aprovar(SALDOS, AGORA, "ev-1")
    r.concluir(aprovada, None)

    lida = r.obter("aut-1")

    assert lida == aprovada.sem_eventos()


def test_concluir_grava_estado_e_eventos_juntos(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    a = nova()
    r.registrar(a)

    r.concluir(a.aprovar(SALDOS, AGORA, "ev-1"), None)

    gravada = r.obter("aut-1")
    assert gravada is not None and gravada.status is StatusAutorizacao.APROVADA
    assert ambiente.eventos_no_outbox() == 1


def test_concluir_sem_ser_dono_do_lease_falha_sem_efeito(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    a = nova()
    r.registrar(a)
    depois = AGORA + DURACAO_DO_LEASE + timedelta(seconds=1)
    assert r.assumir(a.assumir(depois), a.lease_ate)  # outra execução assumiu

    with pytest.raises(LeasePerdido):
        r.concluir(a.aprovar(SALDOS, AGORA, "ev-1"), None)  # lease antigo

    assert ambiente.eventos_no_outbox() == 0


def test_assumir_e_compare_and_set_no_lease(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    a = nova()
    r.registrar(a)
    depois = AGORA + DURACAO_DO_LEASE + timedelta(seconds=1)

    assert r.assumir(a.assumir(depois), a.lease_ate)
    assert not r.assumir(
        a.assumir(depois + timedelta(seconds=30)), a.lease_ate
    )  # lease anterior mudou


def test_regravar_e_compare_and_set_no_lease_de_quem_esta_em_curso(ambiente: Ambiente) -> None:
    """Cada passo da compensação (ADR-0006) só grava se o lease gravado ainda é o que o reconciliador leu."""
    r = ambiente.repositorio
    registro = cast(RegistroDeCompensacao, r)
    a = nova()
    r.registrar(a)
    depois = AGORA + DURACAO_DO_LEASE + timedelta(seconds=1)
    compensando = a.iniciar_compensacao(depois)

    assert registro.regravar(compensando, a.lease_ate)
    assert not registro.regravar(
        compensando.confirmar_aplicacao(depois), a.lease_ate
    )  # lease anterior mudou: outra execução avançou
    confirmada = compensando.confirmar_aplicacao(depois + timedelta(seconds=1))
    assert registro.regravar(confirmada, compensando.lease_ate)

    assert r.obter("aut-1") == confirmada.sem_eventos()


def test_regravar_nao_toca_autorizacao_ja_encerrada(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    a = nova()
    r.registrar(a)
    r.concluir(a.aprovar(SALDOS, AGORA, "ev-1"), None)
    aprovada = r.obter("aut-1")

    assert not cast(RegistroDeCompensacao, r).regravar(
        a.iniciar_compensacao(AGORA + DURACAO_DO_LEASE), a.lease_ate
    )
    assert r.obter("aut-1") == aprovada


def test_estorno_conclui_e_marca_a_original_atomicamente(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    original = nova("orig")
    r.registrar(original)
    r.concluir(aprovada := original.aprovar(SALDOS, AGORA, "ev-1"), None)
    estorno = nova("est", TipoOperacao.ESTORNO, id_autorizacao_original="orig")
    r.registrar(estorno)

    r.concluir(
        estorno.aprovar(SALDOS, AGORA, "ev-2", original=aprovada),
        aprovada.sem_eventos().estornar(AGORA),
    )

    lida = r.obter("orig")
    assert lida is not None and lida.status is StatusAutorizacao.ESTORNADA
    assert ambiente.eventos_no_outbox() == 2


def test_estorno_de_original_ja_encerrada_nao_grava_nada(ambiente: Ambiente) -> None:
    r = ambiente.repositorio
    original = nova("orig")
    r.registrar(original)
    aprovada = original.aprovar(SALDOS, AGORA, "ev-1")
    r.concluir(aprovada, None)
    primeiro = nova("est-1", TipoOperacao.ESTORNO, id_autorizacao_original="orig")
    segundo = nova("est-2", TipoOperacao.ESTORNO, id_autorizacao_original="orig")
    r.registrar(primeiro)
    r.registrar(segundo)
    r.concluir(primeiro.aprovar(SALDOS, AGORA, "ev-2", original=aprovada), aprovada.estornar(AGORA))

    with pytest.raises(OriginalJaEncerrada):
        r.concluir(
            segundo.aprovar(SALDOS, AGORA, "ev-3", original=aprovada), aprovada.estornar(AGORA)
        )

    gravado = r.obter("est-2")
    assert (
        gravado is not None and gravado.status is StatusAutorizacao.EM_PROCESSAMENTO
    )  # nada mudou
    assert ambiente.eventos_no_outbox() == 2  # o evento do perdedor não foi gravado
    r.concluir(segundo.recusar(MotivoRecusa.ORIGINAL_JA_ENCERRADA, None, AGORA, "ev-4"), None)
    assert ambiente.eventos_no_outbox() == 3
