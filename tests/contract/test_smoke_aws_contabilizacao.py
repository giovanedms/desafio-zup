"""O smoke na AWS (`tests/e2e/aws.py corrida`) confere a contabilização (B-5 do painel 6), testado sem a conta.

`tests/e2e/` não é coletado pelo pytest; o módulo é carregado daqui. A AWS é trocada por dublês: o que se prova é a
fiação (quem é esperado, por quanto tempo, com que critério de falha), não o pipeline real, que só a esteira vê.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from limites.adapters.inbound.legado.traducao import traduzir

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("smoke_aws_contabil", RAIZ / "tests/e2e/aws.py")
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)

CONTRATO = "CTR-AWS-TESTE"


def _lancamento(aut: str) -> dict[str, Any]:
    return {
        "historico": {"S": f"Reserva de limite {aut}"},
        "tipo_evento_origem": {"S": "TransacaoAutorizada"},
        "id_evento_origem": {"S": f"ev-{aut}"},
    }


class DynamoFalso:
    """`limites` (get_item) e `contabilizacao-lancamentos` (scan paginado, 1 item por página)."""

    def __init__(self, lancamentos_por_leitura: list[list[dict[str, Any]]]) -> None:
        self.leituras = (
            lancamentos_por_leitura  # cada leitura completa (todas as páginas) devolve a próxima
        )
        self.scans: list[dict[str, Any]] = []
        self._pagina: list[dict[str, Any]] = []

    def get_item(self, **_: Any) -> dict[str, Any]:
        return {
            "Item": {"disponivel": {"N": "0"}, "reservado": {"N": "1000"}, "versao": {"N": "11"}}
        }

    def scan(self, **kw: Any) -> dict[str, Any]:
        self.scans.append(kw)
        if "ExclusiveStartKey" not in kw:
            self._pagina = list(
                self.leituras.pop(0) if len(self.leituras) > 1 else self.leituras[0]
            )
        if not self._pagina:
            return {"Items": []}
        item = self._pagina.pop(0)
        resposta: dict[str, Any] = {"Items": [item]}
        if self._pagina:
            resposta["LastEvaluatedKey"] = {"pk": {"S": item["id_evento_origem"]["S"]}}
        return resposta


def test_lancamentos_do_contrato_pagina_e_filtra_pelo_contrato() -> None:
    dynamo = DynamoFalso([[_lancamento("a"), _lancamento("b"), _lancamento("c")]])

    itens = smoke.lancamentos_do_contrato(dynamo, CONTRATO)

    assert [i["id_evento_origem"]["S"] for i in itens] == ["ev-a", "ev-b", "ev-c"]
    assert len(dynamo.scans) == 3
    primeiro = dynamo.scans[0]
    assert primeiro["TableName"] == "contabilizacao-lancamentos"
    assert primeiro["FilterExpression"] == "id_contrato = :c"
    assert primeiro["ExpressionAttributeValues"] == {":c": {"S": CONTRATO}}
    assert dynamo.scans[2]["ExclusiveStartKey"] == {"pk": {"S": "ev-b"}}


def test_faltam_contabilizar_casa_pelo_id_no_historico() -> None:
    assert smoke.faltam_contabilizar({"a", "b"}, [_lancamento("a")]) == {"b"}
    assert smoke.faltam_contabilizar({"a"}, [_lancamento("a"), _lancamento("x")]) == set()


def test_espera_ate_todos_chegarem() -> None:
    dynamo = DynamoFalso([[], [_lancamento("a")], [_lancamento("a"), _lancamento("b")]])

    faltam, lancamentos = smoke.esperar_contabilizacao(
        dynamo, CONTRATO, {"a", "b"}, dormir=lambda _: None
    )

    assert faltam == set()
    assert len(lancamentos) == 2


def test_desiste_no_prazo_e_diz_o_que_faltou() -> None:
    instantes = iter(range(0, 10_000, 50))
    dynamo = DynamoFalso([[_lancamento("a")]])

    faltam, _ = smoke.esperar_contabilizacao(
        dynamo,
        CONTRATO,
        {"a", "b"},
        prazo_s=120,
        relogio=lambda: float(next(instantes)),
        dormir=lambda _: None,
    )

    assert faltam == {"b"}
    # fim = 0 + 120; o relógio marca 50 e 100 depois das leituras 1 e 2, e 150 depois da 3: desiste.
    assert len(dynamo.scans) == 3


# ---------------------------------------------------------------- corrida() inteira, com a AWS trocada por dublês


def _resposta(status: int, n: int) -> httpx.Response:
    corpo = (
        {"id_autorizacao": f"aut-{n:03d}"} if status == 201 else {"codigo": "LIMITE_INSUFICIENTE"}
    )
    return httpx.Response(status, json=corpo)


@pytest.fixture
def corrida_offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    registro_enviado: list[str] = []
    corpos: list[dict[str, Any]] = []
    contador = iter(range(100))

    def autorizar(_http: Any, _s: Any, _hdr: Any, _contrato: str, corpo: dict[str, Any]) -> Any:
        corpos.append(corpo)
        n = next(contador)
        return _resposta(201 if n < 10 else 402, n), []

    estado: dict[str, Any] = {"registro": registro_enviado, "corpos": corpos, "dynamo": None}
    monkeypatch.setattr(smoke, "saidas", lambda: {"api_keys": {"canal-carga": "k"}})
    monkeypatch.setattr(smoke, "token", lambda *_: "tk")
    monkeypatch.setattr(smoke, "enviar_ao_legado", registro_enviado.extend)
    monkeypatch.setattr(smoke, "esperar_contrato", lambda _: None)
    monkeypatch.setattr(smoke, "autorizar_como_um_canal_correto", autorizar)

    class Sessao:
        def client(self, _servico: str) -> Any:
            return estado["dynamo"]

    monkeypatch.setattr(smoke, "sessao", Sessao)
    monkeypatch.setattr(smoke, "PRAZO_CONTABILIZACAO_S", 0.0)  # uma leitura só: o dublê não muda
    return estado


def test_corrida_passa_com_um_lancamento_por_aprovada(corrida_offline: dict[str, Any]) -> None:
    corrida_offline["dynamo"] = DynamoFalso([[_lancamento(f"aut-{n:03d}") for n in range(10)]])

    smoke.corrida()  # não sai com 1

    assert {c["id_conta"] for c in corrida_offline["corpos"]} == {smoke.CONTA}
    (registro,) = corrida_offline["registro"]
    atualizacao = traduzir(registro)
    assert atualizacao is not None
    assert atualizacao.id_conta == smoke.CONTA  # o contrato carregado é da mesma conta dos pedidos


@pytest.mark.parametrize(
    "lancados",
    [
        pytest.param([f"aut-{n:03d}" for n in range(9)], id="um-faltando"),
        pytest.param([*(f"aut-{n:03d}" for n in range(10)), "aut-000"], id="duplicado"),
        pytest.param([], id="pipeline-parado"),
    ],
)
def test_corrida_falha_se_a_contabilizacao_divergir(
    corrida_offline: dict[str, Any], lancados: list[str]
) -> None:
    corrida_offline["dynamo"] = DynamoFalso([[_lancamento(a) for a in lancados]])

    with pytest.raises(SystemExit) as saida:
        smoke.corrida()

    assert saida.value.code == 1
