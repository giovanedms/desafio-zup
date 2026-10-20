"""Suíte de contrato da porta ExpiracaoDeReservas — a MESMA suíte nos dois adapters (LSP).

No DynamoDB a busca é uma Query nos 10 shards do GSI esparso `gsi_vencimentos`: só RESERVA aprovada e
pendente tem `venc_shard`/`venc_ms`; capturar, estornar ou expirar tira a reserva do índice.
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
from autorizacao.application.portas import (
    ExpiracaoDeReservas,
    OriginalJaEncerrada,
    RepositorioDeAutorizacoes,
)
from autorizacao.domain.autorizacao import (
    VALIDADE_DA_RESERVA,
    Autorizacao,
    MotivoDaReconciliacao,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

    class Repositorio(RepositorioDeAutorizacoes, ExpiracaoDeReservas, Protocol):
        """O que a suíte exercita: gravar/concluir + listar e expirar reservas vencidas."""


AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
VENCIDA = AGORA + VALIDADE_DA_RESERVA + timedelta(seconds=1)
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


def aprovada(
    repositorio: Repositorio, id_autorizacao: str, tipo: TipoOperacao = TipoOperacao.RESERVA
) -> Autorizacao:
    nova = Autorizacao.iniciar(
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("150.00"), "BRL", tipo),
        id_autorizacao=id_autorizacao,
        impressao_digital="imp",
        client_id="canal-pos",
        agora=AGORA,
        correlation_id="corr-1",
        traceparent=None,
    )
    repositorio.registrar(nova)
    final = nova.aprovar(SALDOS, AGORA, f"ev-{id_autorizacao}")
    repositorio.concluir(final, None)
    return final.sem_eventos()


def vencidas(repositorio: Repositorio, esperado: list[str], tentativas: int = 40) -> list[str]:
    """GSI é eventualmente consistente: repete até bater (ou devolve o último resultado)."""
    consulta: Callable[[], list[str]] = lambda: [  # noqa: E731
        a.id_autorizacao for a in repositorio.listar_reservas_vencidas(VENCIDA, 100)
    ]
    resultado = consulta()
    for _ in range(tentativas):
        if resultado == esperado:
            break
        time.sleep(0.25)
        resultado = consulta()
    return resultado


def test_so_reserva_aprovada_e_vencida_aparece(repositorio: Repositorio) -> None:
    aprovada(repositorio, "reserva")
    aprovada(repositorio, "debito", TipoOperacao.DEBITO)

    assert vencidas(repositorio, ["reserva"]) == ["reserva"]
    assert repositorio.listar_reservas_vencidas(VENCIDA - timedelta(seconds=2), 100) == []


def test_reserva_capturada_sai_do_indice(repositorio: Repositorio) -> None:
    reserva = aprovada(repositorio, "reserva")
    assert vencidas(repositorio, ["reserva"]) == ["reserva"]
    captura = Autorizacao.iniciar(
        PedidoDeAutorizacao(
            "CTR-1", "0001-1", Decimal("100.00"), "BRL", TipoOperacao.DEBITO,
            id_autorizacao_original="reserva",
        ),
        id_autorizacao="captura", impressao_digital="imp", client_id="canal-pos",
        agora=AGORA, correlation_id="corr-1", traceparent=None,
    )  # fmt: skip
    repositorio.registrar(captura)

    repositorio.concluir(captura.aprovar(SALDOS, AGORA, "ev-cap", reserva), reserva.capturar(AGORA))

    assert vencidas(repositorio, []) == []
    gravada = repositorio.obter("reserva")
    assert gravada is not None and gravada.status is StatusAutorizacao.CAPTURADA


def test_expirar_grava_o_estado_e_sai_do_indice_uma_vez(repositorio: Repositorio) -> None:
    reserva = aprovada(repositorio, "reserva")
    expirada = reserva.expirar(SALDOS, VENCIDA, "ev-exp")

    repositorio.registrar_expiracao(expirada)

    gravada = repositorio.obter("reserva")
    assert gravada is not None and gravada.status is StatusAutorizacao.EXPIRADA
    assert vencidas(repositorio, []) == []
    # Outra rodada do expirador (outro id de evento) perde o CAS. A MESMA chamada repetida byte a byte
    # seria o retry do SDK, absorvido pelo ClientRequestToken no DynamoDB, e não é o que se testa aqui.
    outra_rodada = reserva.expirar(SALDOS, VENCIDA, "ev-exp-2")
    with pytest.raises(OriginalJaEncerrada):
        repositorio.registrar_expiracao(outra_rodada)


# --- Marca de estorno em curso (mesma porta, mesma suíte nos dois adapters) ------------------------


def test_marca_de_estorno_e_exclusiva_idempotente_e_assumivel(repositorio: Repositorio) -> None:
    aprovada(repositorio, "debito", TipoOperacao.DEBITO)

    assert repositorio.marcar_estorno("debito", "e1") is None
    assert repositorio.marcar_estorno("debito", "e1") is None  # retomada do mesmo estorno
    assert repositorio.marcar_estorno("debito", "e2") == "e1"  # outro estorno: devolve o dono
    assert repositorio.marcar_estorno("debito", "e2", substituir="e1") is None  # marca velha
    assert repositorio.marcar_estorno("debito", "e3", substituir="e1") == "e2"  # CAS: e2 já assumiu


def test_marca_em_original_que_nao_esta_aprovada_devolve_vazio(repositorio: Repositorio) -> None:
    reserva = aprovada(repositorio, "reserva")
    repositorio.registrar_expiracao(reserva.expirar(SALDOS, VENCIDA, "ev-exp"))

    assert repositorio.marcar_estorno("reserva", "e1") == ""
    assert repositorio.marcar_estorno("nao-existe", "e1") == ""


# --- Reserva sem hold vai para a fila humana (status muda, sai do índice, sem TTL, com evento) ------


def test_encaminhar_reserva_sem_hold_muda_status_tira_do_indice_e_publica(
    repositorio: Repositorio,
) -> None:
    reserva = aprovada(repositorio, "reserva")
    encaminhada = reserva.encaminhar_reserva_sem_hold(VENCIDA, "ev-rm")

    repositorio.encaminhar_reserva(encaminhada)

    gravada = repositorio.obter("reserva")
    assert gravada is not None and gravada.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.RESERVA_SEM_HOLD
    assert vencidas(repositorio, []) == []
    with pytest.raises(OriginalJaEncerrada):  # o vencedor da disputa encerrou antes
        repositorio.encaminhar_reserva(reserva.encaminhar_reserva_sem_hold(VENCIDA, "ev-rm-2"))


@pytest.mark.integracao
def test_item_na_fila_humana_fica_sem_ttl_e_no_indice_rm(
    cliente_dynamodb_local: DynamoDBClient, tabelas_autorizacoes: tuple[str, str]
) -> None:
    tabela, outbox = tabelas_autorizacoes
    repositorio = RepositorioDeAutorizacoesDynamoDB(cliente_dynamodb_local, tabela, outbox)
    reserva = aprovada(repositorio, "reserva")

    repositorio.encaminhar_reserva(reserva.encaminhar_reserva_sem_hold(VENCIDA, "ev-rm"))

    item = cliente_dynamodb_local.get_item(
        TableName=tabela, Key={"pk": {"S": "AUT#reserva"}}, ConsistentRead=True
    )["Item"]
    assert "ttl" not in item  # não some em 30 dias enquanto ninguém resolver
    assert item["orfa_shard"]["S"].startswith("RM#")
    assert "venc_shard" not in item
    eventos = cliente_dynamodb_local.scan(TableName=outbox)["Items"]
    assert any(e["detail_type"]["S"] == "TransacaoEmReconciliacaoManual" for e in eventos)
