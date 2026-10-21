"""Busca de órfãs ponta a ponta na LocalStack: Query no GSI esparso gsi_orfas (ADR-0006).

O resto do fluxo (compensar/concluir) já tem teste unitário em tests/unit/test_reconciliador.py; aqui fica o
caso que só aparece com o adapter real: o CloudEvent montado ao gravar no outbox.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from autorizacao.adapters.outbound.dynamodb import RepositorioDeAutorizacoesDynamoDB
from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.portas import EstadoNoLimite, OperacaoNoLimite, ResultadoNoLimite
from autorizacao.application.reconciliar import GRACA, ReconciliarOrfas, ResultadoDaReconciliacao
from autorizacao.domain.autorizacao import DURACAO_DO_LEASE, Autorizacao, StatusAutorizacao
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

pytestmark = pytest.mark.integracao


def test_orfa_com_lease_vencido_ha_2_minutos_e_encontrada(
    cliente_dynamodb_local: DynamoDBClient, tabelas_autorizacoes: tuple[str, str]
) -> None:
    tabela, outbox = tabelas_autorizacoes
    repositorio = RepositorioDeAutorizacoesDynamoDB(cliente_dynamodb_local, tabela, outbox)
    agora = datetime.now(UTC)
    criada_em = agora - timedelta(minutes=2) - DURACAO_DO_LEASE  # lease venceu há 2 min
    orfa = Autorizacao.iniciar(
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("150.00"), "BRL", TipoOperacao.RESERVA),
        id_autorizacao="aut-orfa",
        impressao_digital="imp",
        client_id="canal-pos",
        agora=criada_em,
        correlation_id="corr-1",
        traceparent=None,
    )
    assert repositorio.registrar(orfa) is None

    achadas: list[Autorizacao] = []
    for _ in range(40):  # o GSI é eventualmente consistente
        achadas = repositorio.listar_orfas(agora - GRACA, 100)
        if achadas:
            break
        time.sleep(0.25)

    assert [a.id_autorizacao for a in achadas] == ["aut-orfa"]
    assert achadas[0].status is StatusAutorizacao.EM_PROCESSAMENTO
    assert achadas[0].lease_ate < agora - GRACA


class _LimiteSemEfeito:
    """O limite responde que nada foi aplicado: a órfã compensa sem escrita no limite."""

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        raise AssertionError("nada a desfazer: o reconciliador não deveria escrever no limite")

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        return EstadoNoLimite(na_guarda=False, reserva_ativa=False)


class _Relogio:
    def agora(self) -> datetime:
        return datetime.now(UTC)


class _Ids:
    def novo_id(self) -> str:
        return str(uuid.uuid4())


def test_orfa_com_traceparent_envenenado_compensa_e_nao_para_o_lote(
    cliente_dynamodb_local: DynamoDBClient, tabelas_autorizacoes: tuple[str, str]
) -> None:
    """O PoC G1 do painel: item gravado com `traceparent: X` matava toda rodada no para_cloudevent."""
    tabela, outbox = tabelas_autorizacoes
    repositorio = RepositorioDeAutorizacoesDynamoDB(cliente_dynamodb_local, tabela, outbox)
    criada_em = datetime.now(UTC) - timedelta(minutes=2) - DURACAO_DO_LEASE
    for id_aut, traceparent in (("aut-envenenada", "X"), ("aut-sadia", None)):
        repositorio.registrar(
            Autorizacao.iniciar(
                PedidoDeAutorizacao(
                    "CTR-1", "0001-1", Decimal("150.00"), "BRL", TipoOperacao.RESERVA
                ),
                id_autorizacao=id_aut,
                impressao_digital="imp",
                client_id="canal-pos",
                agora=criada_em,
                correlation_id=str(uuid.uuid4()),
                traceparent=traceparent,
            )
        )
    limite, relogio = _LimiteSemEfeito(), _Relogio()
    autorizar = AutorizarTransacao(repositorio, limite, relogio, _Ids())
    reconciliador = ReconciliarOrfas(repositorio, limite, relogio, autorizar)

    contagem: Counter[ResultadoDaReconciliacao] = Counter()
    for _ in range(40):  # o GSI é eventualmente consistente
        contagem += reconciliador.executar()
        if contagem[ResultadoDaReconciliacao.COMPENSADA] == 2:
            break
        time.sleep(0.25)

    assert contagem == {ResultadoDaReconciliacao.COMPENSADA: 2}
    itens = cliente_dynamodb_local.scan(TableName=outbox)["Items"]
    assert len(itens) == 2
    for item in itens:
        assert "ttl" not in item  # evento não publicado não some por idade
        publicado = json.loads(item["cloudevent"]["S"])
        assert "traceparent" not in publicado
        assert publicado["data"]["trace_id"] is None
