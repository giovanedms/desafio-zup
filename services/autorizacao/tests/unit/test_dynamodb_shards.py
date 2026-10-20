"""Shards dos GSIs esparsos configuráveis (sem rede: botocore Stubber).

O teto que importa é por chave de partição do índice (1.000 WCU/s): N define quantas chaves dividem as escritas.
Subir N tem de ser retrocompatível: o leitor consulta 0..N-1, que contém os shards de qualquer N menor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from autorizacao.adapters.outbound.dynamodb import (
    SHARDS_DE_ORFAS,
    SHARDS_DE_VENCIMENTOS,
    RepositorioDeAutorizacoesDynamoDB,
    _para_item,
    shard_de_orfas,
)
from autorizacao.config import Configuracao
from autorizacao.domain.autorizacao import Autorizacao
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao
from botocore.config import Config
from botocore.stub import ANY, Stubber

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def _cliente() -> tuple[object, Stubber]:
    # Credenciais fictícias explícitas: o Stubber responde antes de qualquer chamada de rede.
    cliente = boto3.client(
        "dynamodb",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 1}),
    )
    return cliente, Stubber(cliente)


def _em_processamento(id_autorizacao: str) -> Autorizacao:
    return Autorizacao.iniciar(
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("10.00"), "BRL", TipoOperacao.RESERVA),
        id_autorizacao=id_autorizacao,
        impressao_digital="imp",
        client_id="canal-pos",
        agora=AGORA,
        correlation_id="corr-1",
        traceparent=None,
    )


def test_padrao_e_64_shards_nos_dois_indices() -> None:
    assert (SHARDS_DE_ORFAS, SHARDS_DE_VENCIMENTOS) == (64, 64)


def test_leitor_consulta_exatamente_os_n_shards_configurados() -> None:
    cliente, stub = _cliente()
    for n in range(3):
        stub.add_response(
            "query",
            {"Items": []},
            {
                "TableName": "autorizacoes",
                "IndexName": "gsi_orfas",
                "KeyConditionExpression": ANY,
                "ExpressionAttributeValues": {":s": {"S": f"EP#{n}"}, ":t": ANY},
                "Limit": 10,
            },
        )
    repositorio = RepositorioDeAutorizacoesDynamoDB(
        cliente,  # type: ignore[arg-type]
        "autorizacoes",
        "autorizacoes-outbox",
        shards_de_orfas=3,
    )

    with stub:
        assert repositorio.listar_orfas(AGORA + timedelta(minutes=1), 10) == []
    stub.assert_no_pending_responses()


def test_escritor_grava_o_shard_dentro_de_n_e_subir_n_cobre_os_shards_antigos() -> None:
    ids = [f"aut-{i}" for i in range(500)]
    for id_autorizacao in ids:
        item = _para_item(_em_processamento(id_autorizacao), 64, 64)
        assert item["orfa_shard"]["S"] == shard_de_orfas(id_autorizacao, 64)
    # Itens gravados com N = 10 caem em EP#0..9, que o leitor com N = 64 consulta (0..63).
    antigos = {shard_de_orfas(i, 10) for i in ids}
    novos_lidos = {f"EP#{n}" for n in range(64)}
    assert antigos <= novos_lidos
    assert len({shard_de_orfas(i, 64) for i in ids}) == 64  # a carga se espalha pelas 64 chaves


def test_numero_de_shards_invalido_e_recusado() -> None:
    cliente, _ = _cliente()
    with pytest.raises(ValueError, match="shards"):
        RepositorioDeAutorizacoesDynamoDB(
            cliente,  # type: ignore[arg-type]
            "autorizacoes",
            "autorizacoes-outbox",
            shards_de_vencimentos=0,
        )


def test_shards_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARDS_DE_ORFAS", "128")
    monkeypatch.setenv("SHARDS_DE_VENCIMENTOS", "32")
    config = Configuracao.do_ambiente()
    assert (config.shards_de_orfas, config.shards_de_vencimentos) == (128, 32)

    monkeypatch.delenv("SHARDS_DE_ORFAS")
    monkeypatch.delenv("SHARDS_DE_VENCIMENTOS")
    config = Configuracao.do_ambiente()
    assert (config.shards_de_orfas, config.shards_de_vencimentos) == (64, 64)
