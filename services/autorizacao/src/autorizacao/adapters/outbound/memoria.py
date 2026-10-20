"""Repositório de autorizações em memória — referência executável das condições do adapter DynamoDB."""

from __future__ import annotations

import threading
from datetime import datetime

from autorizacao.application.portas import LeasePerdido, OriginalJaEncerrada
from autorizacao.domain.autorizacao import (
    EM_CURSO,
    Autorizacao,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import EventoDeDominio


class RepositorioDeAutorizacoesEmMemoria:
    def __init__(self) -> None:
        self._itens: dict[str, Autorizacao] = {}
        self.outbox: list[EventoDeDominio] = []
        self._marcas_de_estorno: dict[str, str] = {}  # id da original → estorno em curso
        self._lock = threading.Lock()

    def obter(self, id_autorizacao: str) -> Autorizacao | None:
        return self._itens.get(id_autorizacao)

    def registrar(self, autorizacao: Autorizacao) -> Autorizacao | None:
        with self._lock:
            existente = self._itens.get(autorizacao.id_autorizacao)
            if existente is not None:
                return existente
            self._itens[autorizacao.id_autorizacao] = autorizacao.sem_eventos()
            return None

    def assumir(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        return self.regravar(autorizacao, lease_anterior)

    def regravar(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        with self._lock:
            gravada = self._itens.get(autorizacao.id_autorizacao)
            if (
                gravada is None
                or gravada.status not in EM_CURSO
                or gravada.lease_ate != lease_anterior
            ):
                return False
            self._itens[autorizacao.id_autorizacao] = autorizacao.sem_eventos()
            return True

    def marcar_estorno(
        self, id_original: str, id_estorno: str, substituir: str | None = None
    ) -> str | None:
        with self._lock:
            original = self._itens.get(id_original)
            if original is None or original.status is not StatusAutorizacao.APROVADA:
                return ""
            dono = self._marcas_de_estorno.get(id_original)
            if dono is not None and dono not in (id_estorno, substituir):
                return dono
            self._marcas_de_estorno[id_original] = id_estorno
            return None

    def listar_orfas(self, lease_vencido_antes_de: datetime, limite: int) -> list[Autorizacao]:
        with self._lock:
            orfas = [
                a
                for a in self._itens.values()
                if a.status in EM_CURSO and a.lease_ate < lease_vencido_antes_de
            ]
        return sorted(orfas, key=lambda a: a.lease_ate)[:limite]

    def concluir(self, autorizacao: Autorizacao, original_encerrada: Autorizacao | None) -> None:
        with self._lock:  # as duas condições e as gravações acontecem juntas (atomicidade)
            gravada = self._itens.get(autorizacao.id_autorizacao)
            if (
                gravada is None
                or gravada.status not in EM_CURSO
                or gravada.lease_ate != autorizacao.lease_ate
            ):
                raise LeasePerdido(autorizacao.id_autorizacao)
            if original_encerrada is not None:
                original = self._itens.get(original_encerrada.id_autorizacao)
                if original is None or original.status is not StatusAutorizacao.APROVADA:
                    raise OriginalJaEncerrada(original_encerrada.id_autorizacao)
                self._itens[original_encerrada.id_autorizacao] = original_encerrada.sem_eventos()
            self._itens[autorizacao.id_autorizacao] = autorizacao.sem_eventos()
            self.outbox.extend(autorizacao.eventos)

    def listar_reservas_vencidas(
        self, vencidas_antes_de: datetime, limite: int
    ) -> list[Autorizacao]:
        with self._lock:
            vencidas = [
                a
                for a in self._itens.values()
                if a.reserva_pendente
                and a.expira_em is not None
                and a.expira_em < vencidas_antes_de
            ]
        return sorted(vencidas, key=lambda a: a.criada_em)[:limite]

    def encaminhar_reserva(self, encaminhada: Autorizacao) -> None:
        with self._lock:
            gravada = self._itens.get(encaminhada.id_autorizacao)
            if gravada is None or gravada.status is not StatusAutorizacao.APROVADA:
                raise OriginalJaEncerrada(encaminhada.id_autorizacao)
            self._itens[encaminhada.id_autorizacao] = encaminhada.sem_eventos()
            self.outbox.extend(encaminhada.eventos)

    def fila_humana(self) -> list[Autorizacao]:
        """O que a query RM# do runbook devolveria (para os testes)."""
        with self._lock:
            return [
                a
                for a in self._itens.values()
                if a.status is StatusAutorizacao.RECONCILIACAO_MANUAL
            ]

    def registrar_expiracao(self, expirada: Autorizacao) -> None:
        with self._lock:
            gravada = self._itens.get(expirada.id_autorizacao)
            if gravada is None or gravada.status is not StatusAutorizacao.APROVADA:
                raise OriginalJaEncerrada(expirada.id_autorizacao)
            self._itens[expirada.id_autorizacao] = expirada.sem_eventos()
            self.outbox.extend(expirada.eventos)
