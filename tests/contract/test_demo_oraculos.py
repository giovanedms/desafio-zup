"""Peças da demo (`tests/e2e/demo.py`) que dão para provar sem a stack: retry de 503/429 com a mesma chave (D8) e
o oráculo da seção 7, que exige a ACL ter julgado a v2 OBSOLETA (E-N5). `tests/e2e/` não é coletado pelo pytest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "infra/local"))  # a demo importa o bootstrap (contratos semeados)
_spec = importlib.util.spec_from_file_location("demo_oraculos", RAIZ / "tests/e2e/demo.py")
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = demo
_spec.loader.exec_module(demo)


def _cliente(respostas: list[httpx.Response], vistos: list[httpx.Request]) -> httpx.Client:
    def responder(pedido: httpx.Request) -> httpx.Response:
        vistos.append(pedido)
        return respostas.pop(0)

    return httpx.Client(transport=httpx.MockTransport(responder))


def test_503_e_429_sao_repetidos_com_a_mesma_chave(monkeypatch: pytest.MonkeyPatch) -> None:
    esperas: list[float] = []
    monkeypatch.setattr(demo.time, "sleep", esperas.append)
    vistos: list[httpx.Request] = []
    respostas = [
        httpx.Response(503, headers={"Retry-After": "1"}),
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(201, json={"id_autorizacao": "a"}),
    ]
    antes = demo.Counter(demo.REPETIDAS)

    with _cliente(respostas, vistos) as http:
        resposta, _ = demo.autorizar(http, "CTR-1", demo.reserva())

    assert resposta.status_code == 201
    assert len({p.headers["Idempotency-Key"] for p in vistos}) == 1  # nunca chave nova no retry
    assert esperas == [1.0, 2.0]  # respeita o Retry-After
    assert demo.REPETIDAS - antes == demo.Counter({503: 1, 429: 1})


def test_retry_tem_teto_e_devolve_o_ultimo_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    vistos: list[httpx.Request] = []
    respostas = [httpx.Response(503, headers={"Retry-After": "x"}) for _ in range(demo.TENTATIVAS)]

    with _cliente(respostas, vistos) as http:
        resposta, _ = demo.autorizar(http, "CTR-1", demo.reserva())

    assert resposta.status_code == 503
    assert len(vistos) == demo.TENTATIVAS


@pytest.mark.parametrize("status", [201, 402, 409, 422])
def test_resposta_final_nao_e_repetida(status: int) -> None:
    vistos: list[httpx.Request] = []
    with _cliente([httpx.Response(status, json={})], vistos) as http:
        resposta, _ = demo.autorizar(http, "CTR-1", demo.reserva())
    assert (resposta.status_code, len(vistos)) == (status, 1)


LOG = """\
limites-acl-1  | 2026-10-21 11:26:47,635 limites.acl {"msg": "lote", "CRIADO": 1}
limites-acl-1  | 2026-10-21 11:26:47,839 limites.acl {"msg": "lote", "ATUALIZADO": 1}
limites-acl-1  | 2026-10-21 11:26:47,889 limites.acl {"msg": "lote", "IGNORADO": 1}
limites-acl-1  | 2026-10-21 11:26:48,000 limites.acl falha ao sincronizar x; fica para reentrega
limites-acl-1  | 2026-10-21 11:26:49,000 limites.acl {"msg": "lote", "FALHA": 1}
limites-acl-1  | {nao é json
"""


def test_fila_vazia_por_ignorado_ou_falha_nao_conta_como_avaliacao() -> None:
    """O falso positivo do painel: registro IGNORADO (ou FALHA) também esvazia a fila; OBSOLETA, não."""
    assert demo.obsoletas_no_log(LOG) == 0


def test_lote_com_obsoleta_e_o_sinal_positivo() -> None:
    linha = 'limites-acl-1  | 2026-10-21 11:27:00,001 limites.acl {"msg": "lote", "OBSOLETA": 1}\n'
    assert demo.obsoletas_no_log(LOG + linha) == 1


def test_contas_dos_pedidos_sao_as_dos_contratos() -> None:
    """CONTA_DIVERGENTE: a demo pede com a conta de cada contrato, tirada de onde o contrato nasce."""
    assert demo.reserva()["id_conta"] == demo.CONTA
    assert demo.conta_semeada("CTR-DEMO-0003") == "0001-00000003-3"
