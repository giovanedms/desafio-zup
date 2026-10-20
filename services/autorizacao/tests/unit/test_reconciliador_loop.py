"""Loop do reconciliador: contagem logada por rodada e composition root (sem rede)."""

from __future__ import annotations

import logging
from collections import Counter

import pytest
from autorizacao.adapters.inbound.reconciliador import uma_rodada
from autorizacao.application.expirar import ExpirarReservas, ResultadoDaExpiracao
from autorizacao.application.reconciliar import ReconciliarOrfas, ResultadoDaReconciliacao
from autorizacao.config import Configuracao
from autorizacao.main import construir_reconciliador


class ReconciliadorFalso:
    def executar(self, limite: int = 100) -> Counter[ResultadoDaReconciliacao]:
        return Counter({ResultadoDaReconciliacao.COMPENSADA: 2, ResultadoDaReconciliacao.FALHOU: 1})


class ExpiradorFalso:
    def executar(self, limite: int = 100) -> Counter[ResultadoDaExpiracao]:
        return Counter({ResultadoDaExpiracao.EXPIRADA: 4})


def test_rodada_devolve_e_loga_a_contagem(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="autorizacao.reconciliador"):
        contagem = uma_rodada(ReconciliadorFalso(), ExpiradorFalso())  # type: ignore[arg-type]

    assert contagem == {"orfa_COMPENSADA": 2, "orfa_FALHOU": 1, "reserva_EXPIRADA": 4}
    assert '"orfas": 3' in caplog.text
    assert '"reservas_vencidas": 4' in caplog.text


def test_intervalo_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALO_RECONCILIADOR_S", "5")

    assert Configuracao.do_ambiente().intervalo_reconciliador_s == 5.0
    monkeypatch.delenv("INTERVALO_RECONCILIADOR_S")
    assert Configuracao.do_ambiente().intervalo_reconciliador_s == 30.0


def test_composition_monta_o_reconciliador_sem_abrir_conexao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")

    reconciliador, expirador = construir_reconciliador(Configuracao.do_ambiente())

    assert isinstance(reconciliador, ReconciliarOrfas)
    assert isinstance(expirador, ExpirarReservas)
