"""Evento de domínio → CloudEvent estruturado (o que vai em `detail` no EventBridge).

Contrato: contracts/events/*.v1.schema.json (verificado por teste de contrato).
- `id` = id_evento gerado no outbox: estável entre reentregas (o `id` do envelope do EventBridge muda).
- `type` com major (`.v1`), `dataschema` com a versão completa, `subject`/`partitionkey` = id_contrato.
- Extensões: `traceparent` (W3C), `correlationid` (nome de atributo CE não aceita `_`).
- Os campos do enunciado (event_id, event_type, event_version, occurred_at, correlation_id, trace_id)
  são espelhados em `data` para aderir literalmente ao contrato pedido.
Dinheiro sai como número JSON exato (simplejson + Decimal), nunca float.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import simplejson

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

SOURCE = "/financas/autorizacao"
_SCHEMAS = "https://financas.example/schemas/eventos"
_SLUGS = {
    TransacaoAutorizada: "transacao-autorizada",
    TransacaoRecusada: "transacao-recusada",
    TransacaoEstornada: "transacao-estornada",
    ReservaExpirada: "reserva-expirada",
    ReservaCapturada: "reserva-capturada",
    TransacaoCompensada: "transacao-compensada",
    TransacaoEmReconciliacaoManual: "transacao-em-reconciliacao-manual",
}
VERSAO_DO_SCHEMA = "1.0"


def nome_curto(evento: EventoDeDominio) -> str:
    return type(evento).__name__


def para_cloudevent(evento: EventoDeDominio) -> dict[str, Any]:
    nome, slug = nome_curto(evento), _SLUGS[type(evento)]
    instante = (
        evento.ocorrido_em.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    trace_id = evento.traceparent.split("-")[1] if evento.traceparent else None
    data: dict[str, Any] = {
        "event_id": evento.id_evento,
        "event_type": nome,
        "event_version": VERSAO_DO_SCHEMA,
        "occurred_at": instante,
        "id_contrato": evento.id_contrato,
        "correlation_id": evento.correlation_id,
        "trace_id": trace_id,
        **_dados_do_negocio(evento),
    }
    envelope: dict[str, Any] = {
        "specversion": "1.0",
        "id": evento.id_evento,
        "source": SOURCE,
        "type": f"example.financas.autorizacao.{nome}.v1",
        "time": instante,
        "subject": evento.id_contrato,
        "datacontenttype": "application/json",
        "dataschema": f"{_SCHEMAS}/{slug}/{VERSAO_DO_SCHEMA}.json",
        "correlationid": evento.correlation_id,
        "partitionkey": evento.id_contrato,
        "data": data,
    }
    if evento.traceparent:
        envelope["traceparent"] = evento.traceparent
    return envelope


def serializar(cloudevent: dict[str, Any]) -> str:
    return str(
        simplejson.dumps(cloudevent, use_decimal=True, ensure_ascii=False, separators=(",", ":"))
    )


def _saldos(saldos: SaldosDoLimite) -> dict[str, Decimal | int]:
    return {
        "saldo_reservado": saldos.saldo_reservado,
        "limite_disponivel": saldos.limite_disponivel,
        "limite_utilizado": saldos.limite_utilizado,
        "versao_limite": saldos.versao,
    }


def _instante(valor: datetime | None) -> str | None:
    if valor is None:
        return None
    return valor.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sem_nulos(dados: dict[str, Any]) -> dict[str, Any]:
    return {chave: valor for chave, valor in dados.items() if valor is not None}


def _dados_do_negocio(evento: EventoDeDominio) -> dict[str, Any]:
    match evento:
        case TransacaoAutorizada():
            return _sem_nulos(
                {
                    "id_autorizacao": evento.id_autorizacao,
                    "valor": evento.valor,
                    "tipo_operacao": evento.tipo_operacao,
                    "moeda": evento.moeda,
                    "id_conta": evento.id_conta,
                    "id_estabelecimento": evento.id_estabelecimento,
                    "id_autorizacao_original": evento.id_autorizacao_original,
                    **_saldos(evento.saldos),
                }
            )
        case TransacaoRecusada():
            dados: dict[str, Any] = {
                "id_autorizacao": evento.id_autorizacao,
                "valor": evento.valor,
                "tipo_operacao": evento.tipo_operacao,
                "motivo": evento.motivo,
                "moeda": evento.moeda,
                "id_conta": evento.id_conta,
                "id_estabelecimento": evento.id_estabelecimento,
            }
            if evento.saldos is not None:
                dados["limite_disponivel"] = evento.saldos.limite_disponivel
                dados["versao_limite"] = evento.saldos.versao
            return _sem_nulos(dados)
        case TransacaoEstornada():
            return {
                "id_autorizacao": evento.id_autorizacao,
                "id_autorizacao_original": evento.id_autorizacao_original,
                "tipo_operacao_original": evento.tipo_operacao_original,
                "valor": evento.valor,
                **_saldos(evento.saldos),
            }
        case ReservaCapturada():
            return {
                "id_reserva": evento.id_reserva,
                "id_autorizacao": evento.id_autorizacao,
                "valor_reservado": evento.valor_reservado,
                "valor_capturado": evento.valor_capturado,
                **_saldos(evento.saldos),
            }
        case TransacaoCompensada():
            return {
                "id_autorizacao": evento.id_autorizacao,
                "tipo_operacao": evento.tipo_operacao,
                "valor": evento.valor,
                "id_conta": evento.id_conta,
                "havia_efeito": evento.havia_efeito,
            }
        case TransacaoEmReconciliacaoManual():
            return _sem_nulos(
                {
                    "id_autorizacao": evento.id_autorizacao,
                    "tipo_operacao": evento.tipo_operacao,
                    "valor": evento.valor,
                    "id_conta": evento.id_conta,
                    "motivo": evento.motivo,
                    "aplicacao_confirmada_em": _instante(evento.aplicacao_confirmada_em),
                    "desfazer_desde": _instante(evento.desfazer_desde),
                }
            )
        case ReservaExpirada():
            return {
                "id_reserva": evento.id_reserva,
                "valor": evento.valor,
                **{k: v for k, v in _saldos(evento.saldos).items() if k != "limite_utilizado"},
            }
    raise TypeError(f"evento sem mapeamento CloudEvents: {type(evento).__name__}")
