"""Repositório em memória: referência executável do contrato da porta (testes e demo sem AWS)."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime

from limites.application.portas import ContratoJaExiste, ContratoNaoEncontrado
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao, sincronizar
from limites.domain.limite import Limite
from limites.domain.operacoes import OperacaoDeLimite
from limites.domain.resultados import Desfecho, ResultadoDaOperacao


class RepositorioDeLimitesEmMemoria:
    """Serializa operações por contrato com um lock — o equivalente local da escrita condicional."""

    def __init__(self) -> None:
        self._limites: dict[str, Limite] = {}
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._lock_do_dicionario = threading.Lock()

    def criar(self, limite: Limite) -> None:
        with self._lock_do_dicionario:
            if limite.id_contrato in self._limites:
                raise ContratoJaExiste(limite.id_contrato)
            self._limites[limite.id_contrato] = limite

    def obter(self, id_contrato: str) -> Limite | None:
        return self._limites.get(id_contrato)

    def aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        with self._lock_de(id_contrato):
            atual = self._limites.get(id_contrato)
            if atual is None:
                raise ContratoNaoEncontrado(id_contrato)
            plano = operacao.planejar()
            if atual.atende(plano):
                novo = atual.aplicar(plano, agora).compactar(
                    agora
                )  # guarda limitada, como no DynamoDB
                self._limites[id_contrato] = novo
                return ResultadoDaOperacao(Desfecho.APLICADA, novo.saldos)
            desfecho, motivo = atual.diagnosticar(operacao)
            return ResultadoDaOperacao(desfecho, atual.saldos, motivo)

    def sincronizar(self, atualizacao: AtualizacaoDoLegado) -> ResultadoDaSincronizacao:
        with self._lock_de(atualizacao.id_contrato):
            novo, resultado = sincronizar(self._limites.get(atualizacao.id_contrato), atualizacao)
            if novo is not None:
                self._limites[atualizacao.id_contrato] = novo
            return resultado

    def _lock_de(self, id_contrato: str) -> threading.Lock:
        with self._lock_do_dicionario:
            return self._locks[id_contrato]
