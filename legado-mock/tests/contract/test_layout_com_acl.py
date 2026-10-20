"""Contrato legado ↔ ACL: tudo que o mock gera é traduzido corretamente pela ACL do BC Limites.

Único lugar do legado-mock que importa `limites`: o mock simula o LEGADO (não depende do BC) e este
teste prova que as duas implementações independentes do layout de 84 posições concordam.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from legado_mock import __main__ as cli
from legado_mock.registro import FUSO_DO_LEGADO, RegistroLC, formatar
from limites.adapters.inbound.legado.traducao import traduzir
from limites.domain.limite import StatusContrato, TipoProduto

PRODUTOS = {
    "01": TipoProduto.CREDITO,
    "02": TipoProduto.INVESTIMENTO,
    "03": TipoProduto.SEGURO,
    "04": TipoProduto.CONSORCIO,
}
SITUACOES = {
    "A": StatusContrato.ATIVO,
    "B": StatusContrato.BLOQUEADO,
    "E": StatusContrato.ENCERRADO,
}
QUANDO = datetime(2026, 10, 20, 16, 30, 5, tzinfo=UTC)


def conferir(r: RegistroLC) -> None:
    traduzido = traduzir(formatar(r))

    assert traduzido is not None
    assert traduzido.id_contrato == r.contrato
    assert traduzido.id_conta == r.conta
    assert traduzido.tipo_produto is PRODUTOS[r.produto]
    assert traduzido.concedido == r.limite
    assert traduzido.status is SITUACOES[r.situacao]
    assert traduzido.versao_legado == r.versao
    # o layout só guarda segundos
    esperado = r.atualizado_em.astimezone(FUSO_DO_LEGADO).replace(microsecond=0)
    assert traduzido.atualizado_em == esperado


@pytest.mark.parametrize("produto", sorted(PRODUTOS))
@pytest.mark.parametrize("situacao", sorted(SITUACOES))
def test_todas_as_combinacoes_de_produto_e_situacao(produto: str, situacao: str) -> None:
    conferir(RegistroLC("CTR-1", "0001-1", produto, Decimal("1234.56"), situacao, 9, QUANDO))


@pytest.mark.parametrize("limite", ["0.00", "0.01", "1500.00", "99999999.99", "99999999999999.99"])
def test_limites_e_decimal_implicito(limite: str) -> None:
    conferir(RegistroLC("CTR-1", "0001-1", "01", Decimal(limite), "A", 1, QUANDO))


def test_campos_nos_tamanhos_maximos() -> None:
    conferir(RegistroLC("C" * 20, "0" * 16, "02", Decimal("1.00"), "E", 10**12 - 1, QUANDO))


@pytest.mark.parametrize("nome", sorted(cli.CENARIOS))
def test_todo_registro_de_todo_cenario_e_traduzido(nome: str) -> None:
    construtor, versao = cli.CENARIOS[nome]

    for r in construtor(versao):
        conferir(r)
