"""O que o outbox publica bate com os schemas de contracts/events (fonte da verdade, contract-first)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from autorizacao.adapters.outbound.cloudevents import _SLUGS, para_cloudevent, serializar
from autorizacao.domain.autorizacao import MotivoDaReconciliacao
from autorizacao.domain.eventos import (
    EventoDeDominio,
    ReservaCapturada,
    ReservaExpirada,
    SaldosDoLimite,
    TransacaoAutorizada,
    TransacaoCompensada,
    TransacaoEmReconciliacaoManual,
    TransacaoEstornada,
    TransacaoRecusada,
)

CONTRATOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
SALDOS = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 42
)
BASE: dict[str, Any] = {
    "id_evento": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
    "ocorrido_em": datetime(2026, 10, 20, 13, 45, 12, 345000, tzinfo=UTC),
    "id_contrato": "CTR-000123456",
    "correlation_id": "0b8a1f2e-9c7d-4e6f-8a5b-3c2d1e0f9a8b",
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
}
ID_AUT = "6f1c2b0e-3d4a-5b6c-8d7e-9f0a1b2c3d4e"

EVENTOS: dict[str, EventoDeDominio] = {
    "transacao-autorizada": TransacaoAutorizada(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="RESERVA",
        valor=Decimal("150.00"),
        moeda="BRL",
        id_conta="0001-1",
        id_estabelecimento=None,
        saldos=SALDOS,
    ),
    "transacao-recusada": TransacaoRecusada(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="DEBITO",
        motivo="LIMITE_INSUFICIENTE",
        valor=Decimal("2000.00"),
        moeda="BRL",
        id_conta="0001-1",
        id_estabelecimento="EST-1",
        saldos=SALDOS,
    ),
    "transacao-estornada": TransacaoEstornada(
        **BASE,
        id_autorizacao=ID_AUT,
        id_autorizacao_original="8b3e4d2a-5f6c-4d7e-8f9a-1b2c3d4e5f6a",
        tipo_operacao_original="RESERVA",
        valor=Decimal("150.00"),
        saldos=SALDOS,
    ),
    "reserva-expirada": ReservaExpirada(
        **BASE, id_reserva=ID_AUT, valor=Decimal("150.00"), saldos=SALDOS
    ),
    "reserva-capturada": ReservaCapturada(
        **BASE,
        id_reserva=ID_AUT,
        id_autorizacao="7a2d3c1f-4e5b-4c6d-9e8f-0a1b2c3d4e5f",
        valor_reservado=Decimal("150.00"),
        valor_capturado=Decimal("120.00"),
        saldos=SALDOS,
    ),
    "transacao-compensada": TransacaoCompensada(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="DEBITO",
        valor=Decimal("150.00"),
        id_conta="0001-1",
        havia_efeito=True,
    ),
    "transacao-em-reconciliacao-manual": TransacaoEmReconciliacaoManual(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="DEBITO",
        valor=Decimal("150.00"),
        id_conta="0001-1",
        motivo=MotivoDaReconciliacao.DESFAZER_FORA_DO_PRAZO.value,
        aplicacao_confirmada_em=datetime(2026, 10, 20, 13, 45, 13, tzinfo=UTC),
        desfazer_desde=datetime(2026, 10, 20, 13, 46, tzinfo=UTC),
    ),
}


def test_captura_cumpre_o_schema_da_transacao_autorizada_com_a_original() -> None:
    captura = TransacaoAutorizada(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="DEBITO",
        valor=Decimal("120.00"),
        moeda="BRL",
        id_conta="0001-1",
        id_estabelecimento="EST-1",
        saldos=SALDOS,
        id_autorizacao_original="8b3e4d2a-5f6c-4d7e-8f9a-1b2c3d4e5f6a",
    )
    publicado = json.loads(serializar(para_cloudevent(captura)))

    assert not list(validador("transacao-autorizada").iter_errors(publicado))
    assert publicado["data"]["id_autorizacao_original"] == "8b3e4d2a-5f6c-4d7e-8f9a-1b2c3d4e5f6a"


def validador(slug: str) -> jsonschema.Draft4Validator:
    schema = json.loads((CONTRATOS / f"{slug}.v1.schema.json").read_text())
    return jsonschema.Draft4Validator(
        schema, format_checker=jsonschema.Draft4Validator.FORMAT_CHECKER
    )


@pytest.mark.parametrize("slug", sorted(EVENTOS))
def test_evento_publicado_cumpre_o_schema(slug: str) -> None:
    publicado = json.loads(serializar(para_cloudevent(EVENTOS[slug])))

    erros = sorted(validador(slug).iter_errors(publicado), key=str)

    assert not erros, [e.message for e in erros]


def test_todo_tipo_publicado_tem_caso_de_contrato() -> None:
    assert set(EVENTOS) == set(_SLUGS.values())


@pytest.mark.parametrize("confirmada", [True, False])
@pytest.mark.parametrize("motivo", list(MotivoDaReconciliacao))
def test_reconciliacao_manual_cumpre_o_schema_em_todo_motivo(
    motivo: MotivoDaReconciliacao, confirmada: bool
) -> None:
    instante = datetime(2026, 10, 20, 13, 45, 13, tzinfo=UTC) if confirmada else None
    evento = TransacaoEmReconciliacaoManual(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="RESERVA",
        valor=Decimal("150.00"),
        id_conta="0001-1",
        motivo=motivo.value,
        aplicacao_confirmada_em=instante,
        desfazer_desde=instante,
    )
    publicado = json.loads(serializar(para_cloudevent(evento)))

    erros = sorted(validador("transacao-em-reconciliacao-manual").iter_errors(publicado), key=str)

    assert not erros, [e.message for e in erros]


def test_campos_do_enunciado_estao_em_data_da_transacao_autorizada() -> None:
    data = json.loads(serializar(para_cloudevent(EVENTOS["transacao-autorizada"])))["data"]

    assert {
        "event_id",
        "event_type",
        "event_version",
        "occurred_at",
        "id_autorizacao",
        "id_contrato",
        "valor",
        "saldo_reservado",
        "correlation_id",
        "trace_id",
    } <= set(data)
    assert data["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_dinheiro_sai_como_numero_exato_no_json() -> None:
    evento = TransacaoAutorizada(
        **BASE,
        id_autorizacao=ID_AUT,
        tipo_operacao="DEBITO",
        valor=Decimal("12345678901234.56"),
        moeda="BRL",
        id_conta="0001-1",
        id_estabelecimento=None,
        saldos=SALDOS,
    )

    texto = serializar(para_cloudevent(evento))

    assert '"valor":12345678901234.56' in texto


def test_id_do_cloudevent_e_o_id_do_evento_de_dominio() -> None:
    publicado = para_cloudevent(EVENTOS["transacao-autorizada"])
    assert publicado["id"] == publicado["data"]["event_id"] == BASE["id_evento"]


@pytest.mark.parametrize(
    "gravado",
    [
        "X",
        "a-DEADBEEF<script>alert(1)</script>",  # 2 partes: virava trace_id e extensão no evento
        "-".join(["00"] * 80),
        BASE["traceparent"].upper(),
    ],
)
@pytest.mark.parametrize("slug", ["transacao-autorizada", "transacao-compensada"])
def test_traceparent_gravado_fora_do_w3c_nao_entra_no_evento(slug: str, gravado: str) -> None:
    """Defesa em profundidade: itens gravados antes da validação na borda podem guardar o header cru."""
    evento = replace(EVENTOS[slug], traceparent=gravado)

    publicado = json.loads(serializar(para_cloudevent(evento)))

    assert not [e.message for e in validador(slug).iter_errors(publicado)]
    assert "traceparent" not in publicado
    assert publicado["data"]["trace_id"] is None


@pytest.mark.parametrize("slug", ["transacao-autorizada", "transacao-recusada"])
def test_canal_sai_no_evento_quando_o_pedido_informou(slug: str) -> None:
    com_canal = json.loads(serializar(para_cloudevent(replace(EVENTOS[slug], canal="POS"))))
    sem_canal = json.loads(serializar(para_cloudevent(EVENTOS[slug])))

    assert not [e.message for e in validador(slug).iter_errors(com_canal)]
    assert com_canal["data"]["canal"] == "POS"
    assert "canal" not in sem_canal["data"]
