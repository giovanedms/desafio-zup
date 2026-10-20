"""Suíte de contrato da porta SincronizacaoComLegado — a MESMA suíte nos dois adapters (LSP).

- memória: sempre; dynamodb: marcado `integracao` (LocalStack).
O ponto delicado é a CORRIDA: reservas (caminho quente) e atualizações do legado escrevem no mesmo
item sem se atrapalhar, e o invariante `disponivel = concedido − utilizado − reservado` sobrevive.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import pytest
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao
from limites.domain.limite import StatusContrato, TipoProduto
from limites.domain.operacoes import Reservar
from limites.domain.resultados import Desfecho

if TYPE_CHECKING:
    from limites.application.portas import RepositorioDeLimites, SincronizacaoComLegado

    class RepositorioCompleto(RepositorioDeLimites, SincronizacaoComLegado, Protocol):
        """O que a suíte exercita: a porta de sincronização + as operações do caminho quente."""


AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def repositorio(request: pytest.FixtureRequest) -> RepositorioCompleto:
    if request.param == "memoria":
        return RepositorioDeLimitesEmMemoria()
    return RepositorioDeLimitesDynamoDB(
        request.getfixturevalue("cliente_dynamodb_local"), request.getfixturevalue("tabela_limites")
    )


def atualizacao(
    versao: int,
    concedido: str = "1000.00",
    status: StatusContrato = StatusContrato.ATIVO,
) -> AtualizacaoDoLegado:
    return AtualizacaoDoLegado(
        id_contrato="CTR-L1",
        id_conta="0001-1",
        tipo_produto=TipoProduto.CREDITO,
        concedido=Decimal(concedido),
        status=status,
        versao_legado=versao,
        atualizado_em=AGORA,
    )


def test_contrato_novo_e_criado(repositorio: RepositorioCompleto) -> None:
    resultado = repositorio.sincronizar(atualizacao(1))

    assert resultado is ResultadoDaSincronizacao.CRIADO
    limite = repositorio.obter("CTR-L1")
    assert limite is not None
    assert (limite.concedido, limite.disponivel, limite.versao_legado) == (
        Decimal("1000.00"),
        Decimal("1000.00"),
        1,
    )


def test_atualizacao_move_o_disponivel_junto(repositorio: RepositorioCompleto) -> None:
    repositorio.sincronizar(atualizacao(1))
    repositorio.aplicar("CTR-L1", Reservar("r1", Decimal("300.00")), AGORA)

    resultado = repositorio.sincronizar(atualizacao(2, "1500.00"))

    assert resultado is ResultadoDaSincronizacao.ATUALIZADO
    limite = repositorio.obter("CTR-L1")
    assert limite is not None
    assert limite.concedido == Decimal("1500.00")
    assert limite.reservado == Decimal("300.00")
    assert limite.disponivel == Decimal("1200.00")
    assert limite.versao_legado == 2
    limite.verificar_invariantes()


def test_mudanca_de_status_e_atualizacao(repositorio: RepositorioCompleto) -> None:
    repositorio.sincronizar(atualizacao(1))

    resultado = repositorio.sincronizar(atualizacao(2, status=StatusContrato.BLOQUEADO))

    assert resultado is ResultadoDaSincronizacao.ATUALIZADO
    limite = repositorio.obter("CTR-L1")
    assert limite is not None
    assert limite.status is StatusContrato.BLOQUEADO


@pytest.mark.parametrize("versao", [1, 2])
def test_versao_menor_ou_igual_e_obsoleta(repositorio: RepositorioCompleto, versao: int) -> None:
    repositorio.sincronizar(atualizacao(1))
    repositorio.sincronizar(atualizacao(2, "1500.00"))

    resultado = repositorio.sincronizar(atualizacao(versao, "9999.00"))

    assert resultado is ResultadoDaSincronizacao.OBSOLETA
    limite = repositorio.obter("CTR-L1")
    assert limite is not None
    assert (limite.concedido, limite.versao_legado) == (Decimal("1500.00"), 2)


def test_versao_nova_sem_mudanca_so_registra_a_versao(
    repositorio: RepositorioCompleto,
) -> None:
    repositorio.sincronizar(atualizacao(1))
    antes = repositorio.obter("CTR-L1")

    resultado = repositorio.sincronizar(atualizacao(2))

    assert resultado is ResultadoDaSincronizacao.SEM_MUDANCA
    depois = repositorio.obter("CTR-L1")
    assert antes is not None
    assert depois is not None
    assert depois.versao_legado == 2
    assert (depois.concedido, depois.disponivel, depois.versao) == (
        antes.concedido,
        antes.disponivel,
        antes.versao,
    )


def test_corrida_de_reservas_com_atualizacoes_do_legado(
    repositorio: RepositorioCompleto,
) -> None:
    """40 reservas de R$ 10 + 5 atualizações do legado (v2..v6) em paralelo: nada se perde."""
    repositorio.sincronizar(atualizacao(1, "1000.00"))

    def reservar(i: int) -> Desfecho:
        return repositorio.aplicar("CTR-L1", Reservar(f"r-{i}", Decimal(10)), AGORA).desfecho

    def sincronizar(versao: int) -> ResultadoDaSincronizacao:
        return repositorio.sincronizar(atualizacao(versao, f"{1000 + 100 * (versao - 1)}.00"))

    with ThreadPoolExecutor(max_workers=24) as pool:
        reservas = [pool.submit(reservar, i) for i in range(40)]
        sincronizacoes = [pool.submit(sincronizar, v) for v in range(2, 7)]
        desfechos = [f.result() for f in reservas]
        for f in sincronizacoes:
            f.result()  # nenhuma pode estourar ContencaoPersistente

    assert desfechos.count(Desfecho.APLICADA) == 40
    final = repositorio.obter("CTR-L1")
    assert final is not None
    final.verificar_invariantes()
    assert final.versao_legado == 6
    assert final.concedido == Decimal("1500.00")
    assert final.reservado == Decimal("400.00")
    assert final.disponivel == final.concedido - final.utilizado - final.reservado
