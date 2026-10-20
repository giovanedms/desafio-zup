"""Suíte de contrato da porta RepositorioDeLimites — a MESMA suíte roda nos dois adapters (LSP).

- memória: sempre (rápido, sem dependências);
- dynamodb: marcado `integracao`, contra a LocalStack (`make up && make test-integracao`).
Se os dois passam, o adapter DynamoDB substitui o de referência sem nenhum chamador perceber.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.portas import ContratoJaExiste, ContratoNaoEncontrado, RepositorioDeLimites
from limites.domain.legado import AtualizacaoDoLegado
from limites.domain.limite import JANELA_DE_DEDUPLICACAO, Limite, StatusContrato, TipoProduto
from limites.domain.operacoes import CapturarReserva, Creditar, Debitar, LiberarReserva, Reservar
from limites.domain.resultados import Desfecho, MotivoRecusa

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def repositorio(request: pytest.FixtureRequest) -> RepositorioDeLimites:
    if request.param == "memoria":
        return RepositorioDeLimitesEmMemoria()
    cliente: DynamoDBClient = request.getfixturevalue("cliente_dynamodb_local")
    return RepositorioDeLimitesDynamoDB(cliente, request.getfixturevalue("tabela_limites"))


def contrato(
    repositorio: RepositorioDeLimites,
    concedido: str = "1000.00",
    status: StatusContrato = StatusContrato.ATIVO,
) -> str:
    limite = Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, Decimal(concedido), status=status)
    repositorio.criar(limite)
    return limite.id_contrato


def test_reserva_aplicada_devolve_saldos_exatos(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)

    resultado = repositorio.aplicar(id_contrato, Reservar("r1", Decimal("150.00")), AGORA)

    assert resultado.desfecho is Desfecho.APLICADA
    assert resultado.saldos.saldo_reservado == Decimal("150.00")
    assert resultado.saldos.limite_disponivel == Decimal("850.00")
    assert resultado.saldos.versao == 2


def test_repetir_operacao_e_ja_aplicada_sem_efeito(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Debitar("d1", Decimal("100.00")), AGORA)

    repetida = repositorio.aplicar(id_contrato, Debitar("d1", Decimal("100.00")), AGORA)

    assert repetida.desfecho is Desfecho.JA_APLICADA
    assert repetida.saldos.limite_utilizado == Decimal("100.00")


def test_limite_insuficiente_e_recusa_sem_efeito(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio, "100.00")

    resultado = repositorio.aplicar(id_contrato, Reservar("r1", Decimal("100.01")), AGORA)

    assert (resultado.desfecho, resultado.motivo) == (
        Desfecho.RECUSADA,
        MotivoRecusa.LIMITE_INSUFICIENTE,
    )
    assert resultado.saldos.limite_disponivel == Decimal("100.00")
    assert resultado.saldos.versao == 1


def test_contrato_bloqueado_recusa_reserva(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio, status=StatusContrato.BLOQUEADO)

    resultado = repositorio.aplicar(id_contrato, Reservar("r1", Decimal("1.00")), AGORA)

    assert resultado.motivo is MotivoRecusa.CONTRATO_INATIVO


def test_credito_acima_do_utilizado_e_recusado(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)

    resultado = repositorio.aplicar(id_contrato, Creditar("c1", Decimal("1.00")), AGORA)

    assert resultado.motivo is MotivoRecusa.CREDITO_EXCEDE_UTILIZADO


def test_liberar_reserva_inexistente_e_recusada(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)

    resultado = repositorio.aplicar(
        id_contrato, LiberarReserva("l1", "nao-existe", Decimal("1.00")), AGORA
    )

    assert resultado.desfecho is Desfecho.RECUSADA
    assert resultado.motivo is MotivoRecusa.RESERVA_NAO_ENCONTRADA


# --- Captura do hold --------------------------------------------------------------------------


def test_captura_total_converte_o_hold_em_utilizado(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)

    resultado = repositorio.aplicar(
        id_contrato, CapturarReserva("c1", "r1", Decimal("400.00"), Decimal("400.00")), AGORA
    )

    assert resultado.desfecho is Desfecho.APLICADA
    assert resultado.saldos.saldo_reservado == Decimal("0.00")
    assert resultado.saldos.limite_utilizado == Decimal("400.00")
    assert resultado.saldos.limite_disponivel == Decimal("600.00")
    limite = repositorio.obter(id_contrato)
    assert limite is not None
    assert "r1" not in limite.reservas
    limite.verificar_invariantes()


def test_captura_parcial_devolve_a_sobra_ao_disponivel(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)

    resultado = repositorio.aplicar(
        id_contrato, CapturarReserva("c1", "r1", Decimal("400.00"), Decimal("250.00")), AGORA
    )

    assert resultado.saldos.saldo_reservado == Decimal("0.00")
    assert resultado.saldos.limite_utilizado == Decimal("250.00")
    assert resultado.saldos.limite_disponivel == Decimal("750.00")


def test_captura_repetida_e_ja_aplicada(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)
    captura = CapturarReserva("c1", "r1", Decimal("400.00"), Decimal("400.00"))
    repositorio.aplicar(id_contrato, captura, AGORA)

    repetida = repositorio.aplicar(id_contrato, captura, AGORA)

    assert repetida.desfecho is Desfecho.JA_APLICADA
    assert repetida.saldos.limite_utilizado == Decimal("400.00")


def test_captura_com_valor_reservado_divergente_e_recusada(
    repositorio: RepositorioDeLimites,
) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)

    resultado = repositorio.aplicar(
        id_contrato, CapturarReserva("c1", "r1", Decimal("300.00"), Decimal("300.00")), AGORA
    )

    assert resultado.motivo is MotivoRecusa.RESERVA_DIVERGENTE


def test_captura_vale_em_contrato_bloqueado_depois_do_hold(
    repositorio: RepositorioDeLimites,
) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)
    limite = repositorio.obter(id_contrato)
    assert limite is not None
    repositorio.sincronizar(
        AtualizacaoDoLegado(
            id_contrato, "0001-1", TipoProduto.CREDITO, Decimal("1000.00"),
            StatusContrato.BLOQUEADO, limite.versao_legado + 1, AGORA,
        )
    )  # fmt: skip

    resultado = repositorio.aplicar(
        id_contrato, CapturarReserva("c1", "r1", Decimal("400.00"), Decimal("400.00")), AGORA
    )

    assert resultado.desfecho is Desfecho.APLICADA


def test_captura_estorno_e_expiracao_disputam_o_hold_e_so_um_vence(
    repositorio: RepositorioDeLimites,
) -> None:
    """O hold é o árbitro: 20 capturas + 10 liberações concorrentes do MESMO hold → exatamente 1 aplica."""
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("400.00")), AGORA)
    disputas = [
        CapturarReserva(f"c{i}", "r1", Decimal("400.00"), Decimal("400.00")) for i in range(20)
    ] + [LiberarReserva(f"l{i}", "r1", Decimal("400.00")) for i in range(10)]

    with ThreadPoolExecutor(max_workers=30) as pool:
        resultados = list(
            pool.map(lambda op: repositorio.aplicar(id_contrato, op, AGORA), disputas)
        )

    desfechos = Counter(r.desfecho for r in resultados)
    assert desfechos[Desfecho.APLICADA] == 1
    assert desfechos[Desfecho.RECUSADA] == 29
    assert {r.motivo for r in resultados if r.motivo} == {MotivoRecusa.RESERVA_NAO_ENCONTRADA}
    final = repositorio.obter(id_contrato)
    assert final is not None
    assert final.reservado == Decimal("0.00")
    assert final.utilizado in {
        Decimal("0.00"),
        Decimal("400.00"),
    }  # capturou OU liberou, nunca os dois
    final.verificar_invariantes()


def test_liberar_com_valor_divergente_e_recusado(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("150.00")), AGORA)

    resultado = repositorio.aplicar(
        id_contrato, LiberarReserva("l1", "r1", Decimal("15.00")), AGORA
    )

    assert resultado.motivo is MotivoRecusa.RESERVA_DIVERGENTE


def test_reservar_e_liberar_devolve_o_disponivel(repositorio: RepositorioDeLimites) -> None:
    id_contrato = contrato(repositorio)
    repositorio.aplicar(id_contrato, Reservar("r1", Decimal("150.00")), AGORA)

    resultado = repositorio.aplicar(
        id_contrato, LiberarReserva("l1", "r1", Decimal("150.00")), AGORA
    )

    assert resultado.saldos.limite_disponivel == Decimal("1000.00")
    assert resultado.saldos.saldo_reservado == Decimal("0.00")


def test_contrato_inexistente(repositorio: RepositorioDeLimites) -> None:
    with pytest.raises(ContratoNaoEncontrado):
        repositorio.aplicar("CTR-X", Reservar("r1", Decimal("1.00")), AGORA)


def test_criar_duas_vezes_e_rejeitado(repositorio: RepositorioDeLimites) -> None:
    contrato(repositorio)

    with pytest.raises(ContratoJaExiste):
        contrato(repositorio)


def test_corrida_de_100_reservas_aprova_exatamente_10(repositorio: RepositorioDeLimites) -> None:
    """Demo 4: 100 reservas paralelas de R$ 100 em R$ 1.000 → 10 aprovadas, 90 recusadas."""
    id_contrato = contrato(repositorio, "1000.00")

    with ThreadPoolExecutor(max_workers=32) as pool:
        resultados = list(
            pool.map(
                lambda i: repositorio.aplicar(id_contrato, Reservar(f"r-{i}", Decimal(100)), AGORA),
                range(100),
            )
        )

    contagem = Counter(r.desfecho for r in resultados)
    assert contagem == {Desfecho.APLICADA: 10, Desfecho.RECUSADA: 90}
    assert all(r.motivo is MotivoRecusa.LIMITE_INSUFICIENTE for r in resultados if r.motivo)
    final = repositorio.obter(id_contrato)
    assert final is not None
    assert final.disponivel == Decimal("0.00")
    assert final.reservado == Decimal("1000.00")
    assert final.versao == 1 + 10


def test_corrida_da_mesma_operacao_aplica_uma_vez(repositorio: RepositorioDeLimites) -> None:
    """Retries concorrentes do MESMO id_operacao (timeout + retry) aplicam exatamente uma vez."""
    id_contrato = contrato(repositorio, "1000.00")

    with ThreadPoolExecutor(max_workers=16) as pool:
        resultados = list(
            pool.map(
                lambda _: repositorio.aplicar(id_contrato, Debitar("d-unico", Decimal(100)), AGORA),
                range(50),
            )
        )

    assert Counter(r.desfecho for r in resultados) == {
        Desfecho.APLICADA: 1,
        Desfecho.JA_APLICADA: 49,
    }
    final = repositorio.obter(id_contrato)
    assert final is not None
    assert final.utilizado == Decimal("100.00")


def test_guarda_de_idempotencia_fica_limitada_a_janela(repositorio: RepositorioDeLimites) -> None:
    """Sem compactação o item cresceria uma entrada por operação até estourar 400 KB (achado na AWS)."""
    id_contrato = contrato(repositorio, "1000000.00")
    for i in range(60):
        repositorio.aplicar(id_contrato, Debitar(f"d-{i}", Decimal("1.00")), AGORA)
    depois = AGORA + JANELA_DE_DEDUPLICACAO + timedelta(seconds=1)

    for i in range(3):
        repositorio.aplicar(id_contrato, Debitar(f"n-{i}", Decimal("1.00")), depois)

    final = repositorio.obter(id_contrato)
    assert final is not None
    assert set(final.operacoes_recentes) == {"n-0", "n-1", "n-2"}
    assert final.utilizado == Decimal("63.00")
    final.verificar_invariantes()
