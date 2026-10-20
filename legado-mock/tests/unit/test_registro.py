"""Formatador do registro de 84 posições e CLI (sem rede): posições, decimal implícito, cenários."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from legado_mock import __main__ as cli
from legado_mock.registro import TAMANHO_DO_REGISTRO, RegistroLC, formatar

QUANDO = datetime(2026, 10, 20, 16, 30, 5, tzinfo=UTC)  # 13:30:05 em Brasília


def registro(**extra: Any) -> RegistroLC:
    base: dict[str, Any] = {
        "contrato": "CTR-1",
        "conta": "0001-1",
        "produto": "01",
        "limite": Decimal("1500.00"),
        "situacao": "A",
        "versao": 7,
        "atualizado_em": QUANDO,
    }
    return RegistroLC(**{**base, **extra})


def test_posicoes_do_layout() -> None:
    texto = formatar(registro())

    assert len(texto) == TAMANHO_DO_REGISTRO
    assert texto[0:2] == "LC"
    assert texto[2:22] == "CTR-1".ljust(20)
    assert texto[22:38] == "0001-1".ljust(16)
    assert texto[38:40] == "01"
    assert texto[40:57] == "00000000000150000"  # 1.500,00 com decimais implícitos
    assert texto[57] == "A"
    assert texto[58:70] == "000000000007"
    assert texto[70:84] == "20261020133005"  # fuso de Brasília


@pytest.mark.parametrize(
    "extra",
    [
        {"produto": "09"},
        {"situacao": "X"},
        {"versao": 0},
        {"limite": Decimal("1.234")},
        {"contrato": "C" * 21},
        {"conta": ""},
    ],
)
def test_campos_invalidos_sao_recusados(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        registro(**extra)


class SqsFalso:
    def __init__(self) -> None:
        self.corpos: list[str] = []

    def get_queue_url(self, QueueName: str) -> dict[str, str]:  # noqa: N803
        return {"QueueUrl": f"http://fila/{QueueName}"}

    def send_message(self, QueueUrl: str, MessageBody: str) -> None:  # noqa: N803
        self.corpos.append(MessageBody)


def test_cenario_fora_de_ordem_envia_v5_v4_v6() -> None:
    construtor, versao = cli.CENARIOS["fora-de-ordem"]
    sqs = SqsFalso()

    cli.enviar(sqs, "legado-contratos", construtor(versao))

    assert [int(c[58:70]) for c in sqs.corpos] == [5, 4, 6]


def test_cenario_carga_inicial_cria_tres_contratos() -> None:
    construtor, versao = cli.CENARIOS["carga-inicial"]

    registros = construtor(versao)

    assert len({r.contrato for r in registros}) == 3
    assert {r.versao for r in registros} == {1}


def test_cenarios_aumento_e_bloqueio() -> None:
    (aumento,) = cli.CENARIOS["aumento"][0](2)
    (bloqueio,) = cli.CENARIOS["bloqueio"][0](3)

    assert (aumento.versao, aumento.limite, aumento.situacao) == (2, Decimal("2500.00"), "A")
    assert (bloqueio.versao, bloqueio.situacao) == (3, "B")
