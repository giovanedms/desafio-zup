"""Suíte de contrato da porta RepositorioContabil: a MESMA suíte nos dois adapters (LSP)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Barrier, Thread
from typing import Any

import pytest
from contabilizacao.adapters.outbound.dynamodb import RepositorioContabilDynamoDB
from contabilizacao.adapters.outbound.memoria import RepositorioContabilEmMemoria
from contabilizacao.application.contabilizar import RepositorioContabil
from contabilizacao.domain.lancamento import Conta, Lancamento, partidas_dobradas

THREADS = 20


@dataclass
class Ambiente:
    repositorio: RepositorioContabil
    total_de_lancamentos: Callable[[], int]


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def ambiente(request: pytest.FixtureRequest) -> Ambiente:
    if request.param == "memoria":
        memoria = RepositorioContabilEmMemoria()
        return Ambiente(memoria, lambda: len(memoria.lancamentos))
    cliente: Any = request.getfixturevalue("cliente_dynamodb_local")
    lancamentos, inbox = request.getfixturevalue("tabelas_contabilizacao")
    repo = RepositorioContabilDynamoDB(cliente, lancamentos, inbox)
    return Ambiente(repo, lambda: len(repo.listar(1000)))


def lancamento(id_evento: str = "evt-1") -> Lancamento:
    return Lancamento(
        id_lancamento=Lancamento.id_para("/financas/autorizacao", id_evento),
        id_evento_origem=id_evento,
        tipo_evento_origem="TransacaoAutorizada",
        id_contrato="CTR-1",
        data_contabil=date(2026, 10, 20),
        ocorrido_em=datetime(2026, 10, 20, 13, 45, tzinfo=UTC),
        historico="Débito autorizado",
        partidas=partidas_dobradas(
            Conta.OPERACOES_DE_CREDITO,
            Conta.OBRIGACOES_COM_ESTABELECIMENTOS,
            Decimal("12345678901234.56"),
        ),
    )


def test_registrar_novo_devolve_true(ambiente: Ambiente) -> None:
    assert ambiente.repositorio.registrar(lancamento(), "src|evt-1") is True
    assert ambiente.total_de_lancamentos() == 1


def test_mesma_chave_devolve_false_e_so_um_lancamento(ambiente: Ambiente) -> None:
    assert ambiente.repositorio.registrar(lancamento(), "src|evt-1") is True
    assert ambiente.repositorio.registrar(lancamento(), "src|evt-1") is False
    assert ambiente.total_de_lancamentos() == 1


def test_chaves_diferentes_gravam_lancamentos_diferentes(ambiente: Ambiente) -> None:
    assert ambiente.repositorio.registrar(lancamento("evt-1"), "src|evt-1") is True
    assert ambiente.repositorio.registrar(lancamento("evt-2"), "src|evt-2") is True
    assert ambiente.total_de_lancamentos() == 2


def test_corrida_de_20_threads_com_o_mesmo_evento_grava_exatamente_um(ambiente: Ambiente) -> None:
    largada = Barrier(THREADS)
    resultados: list[bool] = []
    erros: list[BaseException] = []

    def disputar() -> None:
        try:
            largada.wait()
            resultados.append(ambiente.repositorio.registrar(lancamento(), "src|evt-corrida"))
        except BaseException as erro:
            erros.append(erro)

    threads = [Thread(target=disputar) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not erros
    assert resultados.count(True) == 1
    assert resultados.count(False) == THREADS - 1
    assert ambiente.total_de_lancamentos() == 1
