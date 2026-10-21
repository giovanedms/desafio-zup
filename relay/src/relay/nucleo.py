"""Núcleo do relay: publica eventos pendentes do outbox e apaga só os que saíram.

Semântica at-least-once: um evento pode ser publicado mais de uma vez (relay e sweeper
concorrentes, retry do Lambda, falha ao apagar). Duplicata é normal; o consumidor deduplica por
(source, id) do CloudEvent, e o `id` é estável entre reentregas. Sem dependência de AWS aqui.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

TAMANHO_DO_LOTE = 10  # limite de entradas por PutEvents


@dataclass(frozen=True)
class EventoPendente:
    """Um item OUTBOX# ainda não confirmado como publicado."""

    id_evento: str
    detail_type: str
    cloudevent: str  # JSON já serializado; vai em `Detail` sem reserializar
    criado_em_ms: int


@dataclass(frozen=True)
class ResultadoDoLote:
    publicados: set[str]
    falhos: set[str]


class Publicador(Protocol):
    def publicar(self, lote: Sequence[EventoPendente]) -> set[str]:
        """Devolve os `id_evento` publicados com sucesso. Não levanta por falha de entrega."""
        ...


class Outbox(Protocol):
    def apagar(self, ids: Iterable[str]) -> None: ...

    def paginas_antigas(self, antes_de_ms: int) -> Iterator[list[EventoPendente]]:
        """Itens criados antes de `antes_de_ms`, em páginas de tamanho limitado (sem acumular a tabela)."""
        ...


def processar(
    pendentes: Sequence[EventoPendente], publicador: Publicador, outbox: Outbox
) -> ResultadoDoLote:
    """Publica em lotes de 10, apaga do outbox só os publicados e devolve os falhos."""
    publicados: set[str] = set()
    falhos: set[str] = set()
    for inicio in range(0, len(pendentes), TAMANHO_DO_LOTE):
        lote = pendentes[inicio : inicio + TAMANHO_DO_LOTE]
        ids_do_lote = {e.id_evento for e in lote}
        ok = publicador.publicar(lote) & ids_do_lote
        if ok:
            outbox.apagar(ok)
        publicados |= ok
        falhos |= ids_do_lote - ok
    return ResultadoDoLote(publicados=publicados, falhos=falhos)
