"""Consumidor da ACL do legado: registro → caso de uso, Lambda com batchItemFailures e loop local."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from limites.adapters.inbound.legado import consumidor
from limites.adapters.inbound.legado.traducao import RegistroLegadoInvalido, formatar
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.casos_de_uso import SincronizarComLegado
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao
from limites.domain.limite import StatusContrato, TipoProduto


def registro(versao: int = 1, concedido: str = "1000.00") -> str:
    return formatar(
        AtualizacaoDoLegado(
            "CTR-A1",
            "0001-1",
            TipoProduto.CREDITO,
            Decimal(concedido),
            StatusContrato.ATIVO,
            versao,
            datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
        )
    )


@pytest.fixture
def repositorio() -> RepositorioDeLimitesEmMemoria:
    return RepositorioDeLimitesEmMemoria()


@pytest.fixture
def caso(repositorio: RepositorioDeLimitesEmMemoria) -> SincronizarComLegado:
    return SincronizarComLegado(repositorio)


def test_processar_registro_cria_e_depois_atualiza(
    caso: SincronizarComLegado, repositorio: RepositorioDeLimitesEmMemoria
) -> None:
    assert consumidor.processar_registro(registro(1), caso) is ResultadoDaSincronizacao.CRIADO
    assert (
        consumidor.processar_registro(registro(2, "2500.00"), caso)
        is ResultadoDaSincronizacao.ATUALIZADO
    )
    limite = repositorio.obter("CTR-A1")
    assert limite is not None
    assert limite.concedido == Decimal("2500.00")


def test_tipo_de_registro_ignorado_devolve_none(caso: SincronizarComLegado) -> None:
    outro = "XX" + registro()[2:]

    assert consumidor.processar_registro(outro, caso) is None


def test_registro_invalido_levanta(caso: SincronizarComLegado) -> None:
    with pytest.raises(RegistroLegadoInvalido):
        consumidor.processar_registro("curto demais", caso)


def test_handler_reporta_so_o_item_invalido(
    caso: SincronizarComLegado, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumidor, "_caso_de_uso", caso)
    evento = {
        "Records": [
            {"messageId": "m1", "body": registro(1)},
            {"messageId": "m2", "body": "lixo"},
            {"messageId": "m3", "body": registro(2, "1500.00")},
        ]
    }

    resposta = consumidor.handler(evento, None)

    assert resposta == {"batchItemFailures": [{"itemIdentifier": "m2"}]}


class SqsFalso:
    def __init__(self, corpos: list[str]) -> None:
        self.mensagens = [
            {"MessageId": f"m{i}", "ReceiptHandle": f"rh{i}", "Body": c}
            for i, c in enumerate(corpos)
        ]
        self.apagadas: list[str] = []

    def receive_message(self, **_: Any) -> dict[str, Any]:
        return {"Messages": self.mensagens}

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> None:  # noqa: N803
        self.apagadas.append(ReceiptHandle)


def test_uma_iteracao_apaga_so_o_que_processou(caso: SincronizarComLegado) -> None:
    sqs = SqsFalso([registro(1), "lixo", "XX" + registro()[2:]])

    desfechos = consumidor.uma_iteracao(sqs, "url", caso, espera_s=0)

    assert desfechos == {"CRIADO": 1, "FALHA": 1, "IGNORADO": 1}
    assert sqs.apagadas == ["rh0", "rh2"]  # a inválida fica para o redrive → DLQ
