"""Modo LOCAL: poller da fila SQS da LocalStack. `python -m contabilizacao.adapters.inbound.poller`.

Mesmo caso de uso da Lambda; só a entrada muda. Sucesso (CONTABILIZADO/DUPLICADO/IGNORADO) apaga a
mensagem; exceção NÃO apaga: o SQS reentrega após o visibility timeout e, passado maxReceiveCount,
manda para a DLQ.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from contabilizacao.adapters.inbound.eventbridge_sqs import cloudevent_da_mensagem
from contabilizacao.application.contabilizar import ContabilizarEvento
from contabilizacao.config import do_ambiente
from contabilizacao.main import cliente_aws, construir_caso_de_uso

log = logging.getLogger("contabilizacao.poller")

HEARTBEAT = Path("/tmp/contabilizacao.heartbeat")
ESPERA_S = 10
MAX_MENSAGENS = 10


def uma_iteracao(
    sqs: Any,
    url_fila: str,
    caso_de_uso: ContabilizarEvento,
    espera_s: int = ESPERA_S,
    max_mensagens: int = MAX_MENSAGENS,
) -> Counter[str]:
    """Um long poll: processa o que veio e devolve a contagem por desfecho (FALHA inclusive)."""
    resposta = sqs.receive_message(
        QueueUrl=url_fila, MaxNumberOfMessages=max_mensagens, WaitTimeSeconds=espera_s
    )
    desfechos: Counter[str] = Counter()
    for mensagem in resposta.get("Messages", []):
        try:
            desfecho = caso_de_uso.executar(cloudevent_da_mensagem(mensagem["Body"]))
        except Exception:
            log.exception(
                "falha ao contabilizar %s; fica para reentrega", mensagem.get("MessageId")
            )
            desfechos["FALHA"] += 1
            continue
        sqs.delete_message(QueueUrl=url_fila, ReceiptHandle=mensagem["ReceiptHandle"])
        desfechos[desfecho.value] += 1
    if desfechos:
        log.info(json.dumps({"msg": "lote", **desfechos}))
    return desfechos


def _esperar_fila(sqs: Any, nome: str, tentativas: int = 30) -> str:
    """A fila nasce no bootstrap, que pode terminar depois do container subir."""
    for tentativa in range(1, tentativas + 1):
        try:
            return str(sqs.get_queue_url(QueueName=nome)["QueueUrl"])
        except sqs.exceptions.QueueDoesNotExist:
            log.warning("fila %s ainda não existe (%d/%d)", nome, tentativa, tentativas)
            time.sleep(2)
    sys.exit(f"fila {nome!r} não apareceu; rode o bootstrap")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = do_ambiente()
    if config.endpoint is None:
        sys.exit("AWS_ENDPOINT_URL é obrigatório (modo local)")
    try:
        sqs = cliente_aws("sqs", config)
        caso_de_uso = construir_caso_de_uso(config)
    except ValueError as erro:
        sys.exit(f"recusado: {erro}")
    url_fila = _esperar_fila(sqs, config.fila)
    log.info("poller local iniciado: fila=%s", config.fila)
    while True:
        try:
            uma_iteracao(sqs, url_fila, caso_de_uso)
        except Exception:
            log.exception("iteração falhou; tentando de novo")
            time.sleep(1)
        HEARTBEAT.touch()


if __name__ == "__main__":
    main()
