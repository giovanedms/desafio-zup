"""Repositório contábil em memória: adapter de testes/demos, com a MESMA semântica do DynamoDB."""

from __future__ import annotations

from decimal import Decimal
from threading import Lock

from contabilizacao.domain.lancamento import Conta, Lancamento


class RepositorioContabilEmMemoria:
    def __init__(self) -> None:
        self._trava = Lock()  # o check-and-set da inbox precisa ser atômico (como a transação)
        self._inbox: set[str] = set()
        self._lancamentos: list[Lancamento] = []
        self._ids: set[str] = set()  # o `pk` do lançamento no DynamoDB: também barra a duplicata

    @property
    def lancamentos(self) -> list[Lancamento]:
        with self._trava:
            return list(self._lancamentos)

    def registrar(self, lancamento: Lancamento, chave_da_inbox: str) -> bool:
        with self._trava:
            if chave_da_inbox in self._inbox or lancamento.id_lancamento in self._ids:
                return False
            self._inbox.add(chave_da_inbox)
            self._ids.add(lancamento.id_lancamento)
            self._lancamentos.append(lancamento)
            return True

    def saldos(self) -> dict[Conta, Decimal]:
        total: dict[Conta, Decimal] = {}
        for lancamento in self.lancamentos:
            for conta, efeito in lancamento.saldo_por_conta().items():
                total[conta] = total.get(conta, Decimal(0)) + efeito
        return {conta: valor for conta, valor in total.items() if valor != 0}
