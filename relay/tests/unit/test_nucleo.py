"""processar, varrer e o handler Lambda, com fakes em memória."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import pytest
from relay.lambda_handler import Componentes, handler
from relay.nucleo import EventoPendente, processar
from relay.sweeper import varrer

from relay import lambda_handler


def evento(i: int, criado_em_ms: int = 0) -> EventoPendente:
    return EventoPendente(f"ev-{i}", "TransacaoAutorizada", f'{{"id":"ev-{i}"}}', criado_em_ms)


class PublicadorFake:
    def __init__(self, falhar: set[str] | None = None) -> None:
        self.falhar = falhar or set()
        self.chamadas: list[list[str]] = []

    def publicar(self, lote: Sequence[EventoPendente]) -> set[str]:
        self.chamadas.append([e.id_evento for e in lote])
        return {e.id_evento for e in lote} - self.falhar


class OutboxFake:
    def __init__(self, itens: Sequence[EventoPendente] = ()) -> None:
        self.itens = {e.id_evento: e for e in itens}
        self.apagados: list[str] = []

    def apagar(self, ids: Iterable[str]) -> None:
        for i in ids:
            self.apagados.append(i)
            self.itens.pop(i, None)

    def paginas_antigas(self, antes_de_ms: int) -> Iterator[list[EventoPendente]]:
        yield [e for e in self.itens.values() if e.criado_em_ms < antes_de_ms]


def test_25_eventos_viram_3_chamadas_de_publicar() -> None:
    pendentes = [evento(i) for i in range(25)]
    publicador, outbox = PublicadorFake(), OutboxFake(pendentes)
    resultado = processar(pendentes, publicador, outbox)
    assert [len(c) for c in publicador.chamadas] == [10, 10, 5]
    assert len(resultado.publicados) == 25
    assert resultado.falhos == set()
    assert outbox.itens == {}


def test_publicacao_parcial_apaga_so_os_ok_e_devolve_os_falhos() -> None:
    pendentes = [evento(i) for i in range(5)]
    publicador, outbox = PublicadorFake(falhar={"ev-1", "ev-3"}), OutboxFake(pendentes)
    resultado = processar(pendentes, publicador, outbox)
    assert resultado.falhos == {"ev-1", "ev-3"}
    assert resultado.publicados == {"ev-0", "ev-2", "ev-4"}
    assert set(outbox.itens) == {"ev-1", "ev-3"}


def test_lote_vazio_nao_chama_nada() -> None:
    publicador, outbox = PublicadorFake(), OutboxFake()
    resultado = processar([], publicador, outbox)
    assert publicador.chamadas == []
    assert outbox.apagados == []
    assert resultado.publicados == set()
    assert resultado.falhos == set()


def test_varrer_respeita_idade_minima() -> None:
    agora = 10_000_000
    velho, novo = evento(1, agora - 20 * 60 * 1000), evento(2, agora - 60 * 1000)
    publicador, outbox = PublicadorFake(), OutboxFake([velho, novo])
    resultado = varrer(outbox, publicador, agora)  # padrão: 15 min
    assert resultado.publicados == {"ev-1"}
    assert set(outbox.itens) == {"ev-2"}
    resultado = varrer(outbox, publicador, agora, idade_minima_ms=30_000)
    assert resultado.publicados == {"ev-2"}


def _registro(i: int, seq: str) -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {
                "pk": {"S": f"OUTBOX#ev-{i}"},
                "id_evento": {"S": f"ev-{i}"},
                "detail_type": {"S": "TransacaoAutorizada"},
                "cloudevent": {"S": "{}"},
                "criado_em_ms": {"N": "1"},
            },
        },
    }


def test_handler_devolve_sequence_numbers_dos_falhos(monkeypatch: pytest.MonkeyPatch) -> None:
    publicador, outbox = PublicadorFake(falhar={"ev-1", "ev-2"}), OutboxFake()
    monkeypatch.setattr(
        lambda_handler,
        "_componentes",
        Componentes(publicador=publicador, outbox=outbox),  # type: ignore[arg-type]
    )
    remocao = {"eventName": "REMOVE", "dynamodb": {"SequenceNumber": "999"}}
    evento_lambda = {
        "Records": [_registro(0, "100"), _registro(1, "101"), remocao, _registro(2, "102")]
    }
    resposta = handler(evento_lambda, None)
    assert resposta == {"batchItemFailures": [{"itemIdentifier": "101"}, {"itemIdentifier": "102"}]}
    assert outbox.apagados == ["ev-0"]
