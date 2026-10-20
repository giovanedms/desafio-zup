"""Caso de uso: contabilizar um evento recebido, de forma idempotente (inbox).

Entrega é at-least-once (EventBridge, SQS, relay e sweeper podem reentregar). A inbox grava o id do
evento NA MESMA escrita atômica do lançamento: reentrega vira DUPLICADO, sem lançamento em dobro.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from contabilizacao.domain.lancamento import Lancamento
from contabilizacao.domain.regras import EventoRecebido, LivroDeRegras


class RepositorioContabil(Protocol):
    def registrar(self, lancamento: Lancamento, chave_da_inbox: str) -> bool:
        """Grava inbox + lançamento atomicamente. False se a chave da inbox ou o lançamento (id
        determinístico) já existia: duplicata, inclusive replay do archive depois do TTL da inbox."""
        ...


class Desfecho(StrEnum):
    CONTABILIZADO = "CONTABILIZADO"
    DUPLICADO = "DUPLICADO"
    IGNORADO = "IGNORADO"  # evento sem regra contábil (ex.: TransacaoRecusada)


@dataclass(frozen=True, slots=True)
class ContabilizarEvento:
    repositorio: RepositorioContabil
    regras: LivroDeRegras

    def executar(self, cloudevent: Mapping[str, Any]) -> Desfecho:
        evento = EventoRecebido.de_cloudevent(cloudevent)
        lancamento = self.regras.lancar(evento)
        if lancamento is None:
            return Desfecho.IGNORADO
        chave = f"{evento.source}|{evento.id}"  # dedup pela identidade CloudEvents (source + id)
        if not self.repositorio.registrar(lancamento, chave):
            return Desfecho.DUPLICADO
        return Desfecho.CONTABILIZADO
