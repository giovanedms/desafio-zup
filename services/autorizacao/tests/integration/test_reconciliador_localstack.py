"""Busca de órfãs ponta a ponta na LocalStack: Query no GSI esparso gsi_orfas (ADR-0006).

O resto do fluxo (compensar/concluir) já tem teste unitário em tests/unit/test_reconciliador.py.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from autorizacao.adapters.outbound.dynamodb import RepositorioDeAutorizacoesDynamoDB
from autorizacao.application.reconciliar import GRACA
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
