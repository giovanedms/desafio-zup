"""Contexto de correlação: definir, ler, restaurar e isolamento entre threads."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from plataforma.observabilidade import (
    correlation_id_atual,
    definir_contexto,
    limpar_contexto,
    traceparent_atual,
)

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_padrao_e_none() -> None:
    assert correlation_id_atual.get() is None
    assert traceparent_atual.get() is None


def test_definir_e_limpar_restaura_o_estado_anterior() -> None:
    tokens = definir_contexto("corr-1", TP)
    assert correlation_id_atual.get() == "corr-1"
    assert traceparent_atual.get() == TP

    limpar_contexto(tokens)

    assert correlation_id_atual.get() is None
    assert traceparent_atual.get() is None


def test_aninhado_restaura_o_valor_externo() -> None:
    externo = definir_contexto("externo", None)
    interno = definir_contexto("interno", TP)
    limpar_contexto(interno)
    assert correlation_id_atual.get() == "externo"
    assert traceparent_atual.get() is None
    limpar_contexto(externo)


def test_contexto_copiado_para_thread_e_isolado_de_outras() -> None:
    tokens = definir_contexto("corr-thread", TP)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            copiado = pool.submit(contextvars.copy_context().run, correlation_id_atual.get).result()
            sem_copia = pool.submit(correlation_id_atual.get).result()
        assert copiado == "corr-thread"
        assert sem_copia is None
    finally:
        limpar_contexto(tokens)
