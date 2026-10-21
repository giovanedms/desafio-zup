"""Inbox em memória, com a mesma semântica da InboxDynamoDB (a mesma suíte de contrato roda nas duas)."""

from __future__ import annotations

import threading

from consumidores.nucleo import Situacao

EM_PROCESSAMENTO = "EM_PROCESSAMENTO"
CONCLUIDO = "CONCLUIDO"


class InboxEmMemoria:
    def __init__(self) -> None:
        self.itens: dict[str, tuple[str, int | None]] = {}  # chave -> (estado, prazo_ms)
        self._trava = threading.Lock()

    def reivindicar(self, chave: str, agora_ms: int, prazo_ms: int) -> Situacao:
        with self._trava:
            atual = self.itens.get(chave)
            vencida = (
                atual is not None and atual[0] == EM_PROCESSAMENTO and (atual[1] or 0) < agora_ms
            )
            if atual is None or vencida:
                self.itens[chave] = (EM_PROCESSAMENTO, prazo_ms)
                return Situacao.NOVO
            return Situacao.CONCLUIDO if atual[0] == CONCLUIDO else Situacao.EM_ANDAMENTO

    def concluir(self, chave: str) -> None:
        with self._trava:
            if chave not in self.itens:
                raise KeyError(chave)
            self.itens[chave] = (CONCLUIDO, None)
