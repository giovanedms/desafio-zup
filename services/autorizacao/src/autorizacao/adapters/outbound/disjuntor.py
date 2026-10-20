"""Circuit breaker próprio, thread-safe, com relógio injetável.

Por que existe: quando o BC Limites degrada, cada chamada pendurada segura uma thread do pool (o
caso de uso é síncrono, roda em threadpool) até o timeout. Sem disjuntor, as threads se esgotam e a
API inteira para de responder, inclusive replays e GETs que não dependem do Limites. Aberto, o
disjuntor falha rápido (vira 503 com Retry-After) e deixa o Limites respirar em vez de receber uma
enxurrada de retries.

Estados:
  FECHADO     passa tudo; `limiar_falhas` falhas CONSECUTIVAS abrem (um sucesso zera a contagem).
  ABERTO      rejeita tudo por `pausa_s` segundos.
  MEIO_ABERTO passada a pausa, deixa passar UMA chamada de prova; as concorrentes são rejeitadas.
              Sucesso fecha e zera; falha reabre (nova pausa completa).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum


class EstadoDoDisjuntor(StrEnum):
    FECHADO = "FECHADO"
    ABERTO = "ABERTO"
    MEIO_ABERTO = "MEIO_ABERTO"


class Disjuntor:
    def __init__(
        self,
        limiar_falhas: int = 5,
        pausa_s: float = 5.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limiar = limiar_falhas
        self._pausa = pausa_s
        self._relogio = relogio
        self._lock = threading.Lock()
        self._estado = EstadoDoDisjuntor.FECHADO
        self._falhas = 0
        self._aberto_em = 0.0
        self._prova_em_curso = False

    @property
    def estado(self) -> EstadoDoDisjuntor:
        with self._lock:
            self._avancar_se_pausa_vencida()
            return self._estado

    def permitir(self) -> bool:
        """True se a chamada pode seguir. No MEIO_ABERTO só a primeira recebe True."""
        with self._lock:
            self._avancar_se_pausa_vencida()
            if self._estado is EstadoDoDisjuntor.FECHADO:
                return True
            if self._estado is EstadoDoDisjuntor.MEIO_ABERTO and not self._prova_em_curso:
                self._prova_em_curso = True
                return True
            return False

    def registrar_sucesso(self) -> None:
        with self._lock:
            self._estado = EstadoDoDisjuntor.FECHADO
            self._falhas = 0
            self._prova_em_curso = False

    def registrar_falha(self) -> None:
        with self._lock:
            if self._estado is EstadoDoDisjuntor.MEIO_ABERTO:
                self._abrir()
                return
            self._falhas += 1
            if self._estado is EstadoDoDisjuntor.FECHADO and self._falhas >= self._limiar:
                self._abrir()

    def _abrir(self) -> None:
        self._estado = EstadoDoDisjuntor.ABERTO
        self._aberto_em = self._relogio()
        self._prova_em_curso = False

    def _avancar_se_pausa_vencida(self) -> None:
        if (
            self._estado is EstadoDoDisjuntor.ABERTO
            and self._relogio() - self._aberto_em >= self._pausa
        ):
            self._estado = EstadoDoDisjuntor.MEIO_ABERTO
            self._prova_em_curso = False
