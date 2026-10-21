"""Suíte de contrato da porta Inbox: a MESMA suíte na memória e no DynamoDB (LocalStack), para a memória dos testes
unitários não prometer o que o DynamoDB não cumpre."""

from __future__ import annotations

import time
from threading import Barrier, Thread
from typing import Any

import pytest
from botocore.exceptions import ClientError
from consumidores.inbox_dynamodb import InboxDynamoDB
from consumidores.inbox_memoria import InboxEmMemoria
from consumidores.nucleo import Inbox, Situacao

AGORA_MS = 1_790_000_000_000
PRAZO_MS = AGORA_MS + 30_000
CHAVE = "/financas/autorizacao|a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
THREADS = 20


@pytest.fixture(params=["memoria", pytest.param("dynamodb", marks=pytest.mark.integracao)])
def inbox(request: pytest.FixtureRequest) -> Inbox:
    if request.param == "memoria":
        return InboxEmMemoria()
    cliente: Any = request.getfixturevalue("cliente_dynamodb_local")
    return InboxDynamoDB(cliente, request.getfixturevalue("tabela_inbox"))


def test_chave_nova_e_reivindicada(inbox: Inbox) -> None:
    assert inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS) is Situacao.NOVO


def test_reivindicacao_viva_bloqueia_outra(inbox: Inbox) -> None:
    inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS)

    assert inbox.reivindicar(CHAVE, AGORA_MS + 1_000, PRAZO_MS + 1_000) is Situacao.EM_ANDAMENTO
    # no limite (agora == prazo) a reivindicação ainda vale
    assert inbox.reivindicar(CHAVE, PRAZO_MS, PRAZO_MS + 30_000) is Situacao.EM_ANDAMENTO


def test_concluida_e_duplicata_para_sempre(inbox: Inbox) -> None:
    inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS)
    inbox.concluir(CHAVE)

    assert inbox.reivindicar(CHAVE, AGORA_MS + 1, PRAZO_MS) is Situacao.CONCLUIDO
    # concluída, o prazo não conta mais
    assert inbox.reivindicar(CHAVE, PRAZO_MS + 86_400_000, PRAZO_MS) is Situacao.CONCLUIDO


def test_reivindicacao_vencida_sem_conclusao_volta_a_ser_nova(inbox: Inbox) -> None:
    inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS)  # a invocação morreu sem concluir

    assert inbox.reivindicar(CHAVE, PRAZO_MS + 1, PRAZO_MS + 30_001) is Situacao.NOVO
    # a nova reivindicação tem o prazo NOVO: outra chegada antes dele continua bloqueada
    assert inbox.reivindicar(CHAVE, PRAZO_MS + 2, PRAZO_MS + 30_002) is Situacao.EM_ANDAMENTO


def test_chaves_sao_independentes(inbox: Inbox) -> None:
    inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS)
    inbox.concluir(CHAVE)

    assert (
        inbox.reivindicar("/financas/limites|" + CHAVE.split("|")[1], AGORA_MS, PRAZO_MS)
        is Situacao.NOVO
    )


def test_concluir_sem_reivindicar_falha(inbox: Inbox) -> None:
    with pytest.raises((KeyError, ClientError)):
        inbox.concluir(CHAVE)


def test_corrida_de_20_invocacoes_pelo_mesmo_evento_tem_um_vencedor(inbox: Inbox) -> None:
    largada = Barrier(THREADS)
    situacoes: list[Situacao] = []

    def disputar() -> None:
        largada.wait()
        situacoes.append(inbox.reivindicar(CHAVE, AGORA_MS, PRAZO_MS))

    threads = [Thread(target=disputar) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(situacoes) == sorted([Situacao.NOVO] + [Situacao.EM_ANDAMENTO] * (THREADS - 1))


@pytest.mark.integracao
def test_item_no_dynamodb_tem_ttl_de_30_dias_e_perde_o_prazo_ao_concluir(
    cliente_dynamodb_local: Any, tabela_inbox: str
) -> None:
    inbox = InboxDynamoDB(cliente_dynamodb_local, tabela_inbox)
    agora_ms = time.time_ns() // 1_000_000
    inbox.reivindicar(CHAVE, agora_ms, agora_ms + 30_000)

    chave = {"pk": {"S": f"INBOX#{CHAVE}"}}
    reivindicado = cliente_dynamodb_local.get_item(
        TableName=tabela_inbox, Key=chave, ConsistentRead=True
    )["Item"]
    inbox.concluir(CHAVE)
    concluido = cliente_dynamodb_local.get_item(
        TableName=tabela_inbox, Key=chave, ConsistentRead=True
    )["Item"]

    assert int(reivindicado["ttl"]["N"]) == agora_ms // 1000 + 30 * 86_400
    assert reivindicado["estado"]["S"] == "EM_PROCESSAMENTO" and "prazo_ms" in reivindicado
    assert concluido["estado"]["S"] == "CONCLUIDO" and "prazo_ms" not in concluido
    assert concluido["ttl"] == reivindicado["ttl"]
