"""Entrada AWS: Lambda com event source mapping da fila do consumidor (ReportBatchItemFailures).

Uma função por fila (antifraude, notificacoes), mesmo código: `CONSUMIDOR` diz qual é e `TABELA_INBOX`, a inbox dela.
Os schemas vêm em `consumidores/esquemas/`, copiados de contracts/events por `scripts/empacotar_lambdas.sh`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.inbox_dynamodb import InboxDynamoDB
from consumidores.nucleo import ConsumirEvento, Desfecho, EventoInvalido

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

DIRETORIO_DOS_ESQUEMAS = Path(__file__).resolve().parent / "esquemas"
# Prazo da reivindicação quando não há contexto do Lambda (fora dele): o timeout padrão das funções no Terraform.
PRAZO_SEM_CONTEXTO_MS = 30_000

# Endpoint só o da AWS: `AWS_ENDPOINT_URL[_<SERVIÇO>]` na configuração da função não desvia o cliente (mesma trava do
# relay e dos composition roots dos serviços).
SO_ENDPOINT_DA_AWS = Config(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
    ignore_configured_endpoint_urls=True,
)

_consumir: ConsumirEvento | None = None


def _emitir(linha: dict[str, Any]) -> None:
    log.info(json.dumps(linha, ensure_ascii=False))


def _agora_ms() -> int:
    return time.time_ns() // 1_000_000


def consumir_evento() -> ConsumirEvento:
    """Montado uma vez por container e FORA do try de cada mensagem: variável, tabela ou schema faltando derruba a
    invocação (alarme `lambda-erros-<função>`, runbook R12), em vez de virar falha silenciosa de cada mensagem."""
    global _consumir  # noqa: PLW0603
    if _consumir is None:
        _consumir = ConsumirEvento(
            consumidor=os.environ["CONSUMIDOR"],
            validador=CatalogoDeEsquemas.do_diretorio(DIRETORIO_DOS_ESQUEMAS),
            inbox=InboxDynamoDB(
                boto3.client(
                    "dynamodb", region_name=os.environ.get("AWS_REGION"), config=SO_ENDPOINT_DA_AWS
                ),
                os.environ["TABELA_INBOX"],
            ),
            emitir=_emitir,
            relogio_ms=_agora_ms,
        )
    return _consumir


def prazo_da_invocacao_ms(contexto: object, agora_ms: int) -> int:
    """Fim desta invocação, em epoch ms. Depois dele a função não roda mais: a reivindicação pode vencer."""
    restante = getattr(contexto, "get_remaining_time_in_millis", None)
    return agora_ms + (int(restante()) if callable(restante) else PRAZO_SEM_CONTEXTO_MS)


def handler(evento: dict[str, Any], contexto: object) -> dict[str, list[dict[str, str]]]:
    consumir = consumir_evento()
    prazo_ms = prazo_da_invocacao_ms(contexto, consumir.relogio_ms())
    falhas: list[dict[str, str]] = []
    desfechos: Counter[str] = Counter()
    for registro in evento.get("Records", []):
        message_id = registro["messageId"]
        try:
            desfecho = consumir.executar(registro["body"], prazo_ms)
        except EventoInvalido as erro:
            # Só caminho e regra (sem valor): o messageId acha a mensagem na DLQ, que o preserva.
            log.warning(
                json.dumps(
                    {
                        "evento": "evento_invalido",
                        "consumidor": consumir.consumidor,
                        "message_id": message_id,
                        "erros": list(erro.erros),
                    }
                )
            )
            desfechos["INVALIDO"] += 1
            falhas.append({"itemIdentifier": message_id})
            continue
        except Exception:
            # Inbox fora, throttling, permissão: só ESTE item volta (retry e, esgotado, DLQ); o resto do lote segue.
            log.exception("falha ao consumir a mensagem %s", message_id)
            desfechos["FALHA"] += 1
            falhas.append({"itemIdentifier": message_id})
            continue
        desfechos[desfecho.value] += 1
        if desfecho is Desfecho.EM_ANDAMENTO:
            falhas.append({"itemIdentifier": message_id})
    log.info(json.dumps({"msg": "lote", "consumidor": consumir.consumidor, **desfechos}))
    return {"batchItemFailures": falhas}
