"""Sweeper incremental: outbox grande drenado em páginas, com teto e prazo por rodada (B-1/F-2 do painel 6).

O DynamoDB falso serve o Scan como o real: `Limit` = itens AVALIADOS por página, `LastEvaluatedKey` enquanto
houver mais, `FilterExpression` aplicado depois do Limit. O código do outbox e do sweeper é o real.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import pytest
from relay.lambda_handler import Componentes
from relay.nucleo import EventoPendente
from relay.outbox_dynamodb import ITENS_POR_PAGINA, OutboxDynamoDB
from relay.sweeper import ORCAMENTO_S, TETO_DE_ITENS, _prazo, handler_agendado, varrer

from relay import lambda_handler

AGORA_MS = 10_000_000_000
VELHO_MS = AGORA_MS - 60 * 60 * 1000  # 1 h: passou da idade mínima de 15 min
NOVO_MS = AGORA_MS - 60 * 1000  # 1 min: o relay ainda deve pegar pelo Stream


class DynamoFalso:
    """Tabela de outbox em memória, em ordem de chave estável (a ordem de hash do Scan real)."""

    def __init__(self, itens: dict[str, int], diario: list[str]) -> None:
        self.tabela = {f"OUTBOX#{i}": criado for i, criado in itens.items()}
        self.diario = diario
        self.limites_pedidos: list[int] = []
        self.maior_pagina = 0

    def scan(self, **kw: Any) -> dict[str, Any]:
        self.diario.append("scan")
        self.limites_pedidos.append(kw["Limit"])
        chaves = sorted(self.tabela)
        inicio = 0
        if "ExclusiveStartKey" in kw:
            ultima = kw["ExclusiveStartKey"]["pk"]["S"]
            inicio = next((n for n, c in enumerate(chaves) if c > ultima), len(chaves))
        avaliadas = chaves[inicio : inicio + kw["Limit"]]
        corte = int(kw["ExpressionAttributeValues"][":t"]["N"])
        itens = [
            {
                "pk": {"S": c},
                "id_evento": {"S": c.removeprefix("OUTBOX#")},
                "detail_type": {"S": "TransacaoAutorizada"},
                "cloudevent": {"S": "{}"},
                "criado_em_ms": {"N": str(self.tabela[c])},
            }
            for c in avaliadas
            if self.tabela[c] < corte
        ]
        self.maior_pagina = max(self.maior_pagina, len(itens))
        resposta: dict[str, Any] = {"Items": itens}
        if inicio + kw["Limit"] < len(chaves):
            resposta["LastEvaluatedKey"] = {"pk": {"S": avaliadas[-1]}}
        return resposta

    def batch_write_item(self, RequestItems: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        for pedidos in RequestItems.values():
            for p in pedidos:
                self.tabela.pop(p["DeleteRequest"]["Key"]["pk"]["S"], None)
        return {"UnprocessedItems": {}}


class PublicadorFalso:
    def __init__(self, diario: list[str], falhar: set[str] | None = None) -> None:
        self.diario = diario
        self.falhar = falhar or set()
        self.publicados: list[str] = []

    def publicar(self, lote: Sequence[EventoPendente]) -> set[str]:
        self.diario.append("publicar")
        ok = {e.id_evento for e in lote} - self.falhar
        self.publicados += sorted(ok)
        return ok


def _cenario(
    velhos: int, novos: int = 0, falhar: set[str] | None = None, agora_ms: int = AGORA_MS
) -> tuple[DynamoFalso, OutboxDynamoDB, PublicadorFalso, list[str]]:
    diario: list[str] = []
    itens = {f"v{n:06d}": VELHO_MS - AGORA_MS + agora_ms for n in range(velhos)}
    itens |= {f"n{n:06d}": NOVO_MS - AGORA_MS + agora_ms for n in range(novos)}
    dynamo = DynamoFalso(itens, diario)
    return dynamo, OutboxDynamoDB(dynamo, "outbox"), PublicadorFalso(diario, falhar), diario  # type: ignore[arg-type]


def test_outbox_grande_publica_antes_de_ler_a_segunda_pagina() -> None:
    """O PoC do painel: 45.000 itens e ZERO publicações (OOM no Scan inteiro). Agora publica por página."""
    dynamo, outbox, publicador, diario = _cenario(45_000)

    resultado = varrer(outbox, publicador, AGORA_MS)

    assert diario.index("publicar") < diario.index("scan", 1)  # publicou a 1a página antes da 2a
    assert set(dynamo.limites_pedidos) == {ITENS_POR_PAGINA}  # todo Scan com Limit
    assert dynamo.maior_pagina <= ITENS_POR_PAGINA  # memória por página limitada
    assert resultado.interrompida  # parou no teto: o resto fica para a próxima rodada
    assert resultado.varridos == len(resultado.publicados) == TETO_DE_ITENS
    assert diario.count("scan") == TETO_DE_ITENS // ITENS_POR_PAGINA
    assert len(dynamo.tabela) == 45_000 - TETO_DE_ITENS  # o publicado saiu do outbox


def test_rodadas_sucessivas_drenam_tudo_uma_vez_e_nao_tocam_no_que_e_novo() -> None:
    dynamo, outbox, publicador, _ = _cenario(25_000, novos=300)

    rodadas = 0
    while True:
        rodadas += 1
        resultado = varrer(outbox, publicador, AGORA_MS, teto_de_itens=8_000)
        if not resultado.interrompida:
            break

    assert rodadas == 4  # 8.000 + 8.000 + 8.000 + 1.000
    assert len(publicador.publicados) == len(set(publicador.publicados)) == 25_000
    assert set(dynamo.tabela) == {f"OUTBOX#n{n:06d}" for n in range(300)}


def test_orcamento_de_tempo_para_entre_paginas() -> None:
    dynamo, outbox, publicador, diario = _cenario(10_000)
    instantes = iter(range(0, 1000, 15))  # cada consulta ao relógio avança 15 s

    resultado = varrer(
        outbox, publicador, AGORA_MS, prazo_s=40, relogio=lambda: float(next(instantes))
    )

    # fim = 0 + 40; após a 1a página o relógio marca 15, após a 2a 30, após a 3a 45 → para.
    assert resultado.interrompida
    assert diario.count("scan") == 3
    assert resultado.varridos == 3 * ITENS_POR_PAGINA
    assert len(dynamo.tabela) == 10_000 - 3 * ITENS_POR_PAGINA


def test_tabela_grande_sem_item_antigo_respeita_o_prazo() -> None:
    """Páginas vazias (o filtro descarta tudo) também contam no prazo: o Scan não roda até o fim da tabela."""
    _, outbox, publicador, diario = _cenario(0, novos=5_000)
    instantes = iter(range(0, 1000, 15))

    resultado = varrer(
        outbox, publicador, AGORA_MS, prazo_s=40, relogio=lambda: float(next(instantes))
    )

    assert resultado.interrompida
    assert diario.count("scan") == 3
    assert resultado.varridos == 0
    assert "publicar" not in diario


def test_teto_conta_so_item_antigo_e_a_rodada_chega_ao_antigo_atras_dos_novos() -> None:
    """F7-6: `varridos` e o teto contam o que o filtro devolveu, não o ScannedCount. Com 3.000 itens novos na
    frente (ordem do Scan) e teto de 1.000, um teto em itens avaliados pararia na 2a página em toda rodada (o Scan
    recomeça do início) e os 100 antigos nunca sairiam. Aqui a rodada lê as 7 páginas e publica os 100."""
    dynamo, outbox, publicador, diario = _cenario(100, novos=3_000)
    assert sorted(dynamo.tabela)[0].startswith("OUTBOX#n")  # os novos vêm antes no Scan

    resultado = varrer(outbox, publicador, AGORA_MS, teto_de_itens=1_000)

    assert diario.count("scan") == 7  # 3.100 avaliados em páginas de 500: passou de 1.000 avaliados
    assert (resultado.varridos, len(resultado.publicados), resultado.interrompida) == (
        100,
        100,
        False,
    )
    assert len(dynamo.tabela) == 3_000  # só os novos ficaram


def test_falha_de_publicacao_fica_no_outbox_para_a_proxima_rodada() -> None:
    dynamo, outbox, publicador, _ = _cenario(30, falhar={"v000007"})

    resultado = varrer(outbox, publicador, AGORA_MS)

    assert not resultado.interrompida
    assert resultado.falhos == {"v000007"}
    assert len(resultado.publicados) == 29
    assert set(dynamo.tabela) == {"OUTBOX#v000007"}


class Contexto:
    def __init__(self, restante_ms: int) -> None:
        self.restante_ms = restante_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.restante_ms


@pytest.mark.parametrize(
    ("contexto", "esperado"),
    [
        (None, ORCAMENTO_S),  # fora da Lambda: o orçamento fixo
        (Contexto(60_000), ORCAMENTO_S),  # timeout de 60 s: 60 − 15 de margem > 40
        (Contexto(30_000), 15.0),  # timeout menor na IaC: o prazo encolhe junto
        (Contexto(5_000), 0.0),  # sem folga: lê uma página e para
    ],
)
def test_prazo_respeita_o_tempo_restante_da_lambda(contexto: object, esperado: float) -> None:
    assert _prazo(contexto) == esperado


def test_handler_agendado_varre_publica_e_loga(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agora_ms = int(time.time() * 1000)  # o handler usa o relógio de verdade
    dynamo, outbox, publicador, _ = _cenario(1_200, novos=10, falhar={"v000003"}, agora_ms=agora_ms)
    monkeypatch.setattr(
        lambda_handler,
        "_componentes",
        Componentes(publicador=publicador, outbox=outbox),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.INFO, logger="relay.sweeper"):
        resposta = handler_agendado({}, Contexto(60_000))

    assert resposta == {"publicados": 1_199, "falhos": 1, "varridos": 1_200, "interrompida": False}
    assert len(dynamo.tabela) == 11  # 10 novos + o que falhou
    linha = json.loads(caplog.records[-1].getMessage())
    assert linha == {"msg": "sweeper", **resposta}
