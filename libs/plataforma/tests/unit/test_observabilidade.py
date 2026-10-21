"""Contexto de correlação: definir, ler, restaurar e isolamento entre threads."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest
from plataforma.observabilidade import (
    contexto_de_log,
    correlation_id_atual,
    definir_contexto,
    gateway_request_id_atual,
    limpar_contexto,
    trace_id_de,
    traceparent_atual,
    traceparent_valido,
)

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_padrao_e_none() -> None:
    assert correlation_id_atual.get() is None
    assert traceparent_atual.get() is None
    assert gateway_request_id_atual.get() is None
    assert contexto_de_log() == {}


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


def test_gateway_request_id_entra_no_contexto_de_log_e_sai_ao_limpar() -> None:
    tokens = definir_contexto("corr-1", TP, "c6af9ac6-7b61-11e6-9a41-93e8deadbeef")
    try:
        assert gateway_request_id_atual.get() == "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"
        assert contexto_de_log() == {
            "correlation_id": "corr-1",
            "gateway_request_id": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef",
        }
    finally:
        limpar_contexto(tokens)

    assert gateway_request_id_atual.get() is None


def test_sem_gateway_o_contexto_de_log_so_tem_a_correlacao() -> None:
    tokens = definir_contexto("corr-local", None)
    try:
        assert contexto_de_log() == {"correlation_id": "corr-local"}
    finally:
        limpar_contexto(tokens)


def test_traceparent_w3c_valido_passa_e_da_o_trace_id() -> None:
    assert traceparent_valido(TP) == TP
    assert trace_id_de(TP) == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(
    "hostil",
    [
        None,
        "",
        "X",  # sem hífen: o split("-")[1] antigo levantava IndexError
        "a-DEADBEEF<script>alert(1)</script>",  # 2 partes: virava trace_id no evento
        "-".join(["00"] * 80),  # 80 partes
        TP.upper(),  # W3C exige hex minúsculo
        TP + "\n",  # `$` de regex aceitaria a quebra de linha no fim
        TP + "-extra",
    ],
)
def test_traceparent_fora_do_formato_vira_none(hostil: str | None) -> None:
    assert traceparent_valido(hostil) is None
    assert trace_id_de(hostil) is None
