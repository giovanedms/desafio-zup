"""Entradas da ACL do legado: Lambda (SQS) na AWS e loop de long polling local.

`python -m limites.adapters.inbound.legado.consumidor` (modo local, LocalStack).
Os dois caminhos usam `processar_registro`: traduzir (ACL) → executar o caso de uso.
Na AWS o mesmo loop roda como serviço ECS (`limites-acl`, ACL_MODO_AWS=true, credenciais da task role).
Registro inválido levanta `RegistroLegadoInvalido`: não é apagado, o SQS reentrega e, após
maxReceiveCount, a mensagem vai para a DLQ (nunca é descartada em silêncio).
Heartbeat em /tmp/limites-acl.heartbeat (healthcheck do container, < 30 s), renovado SÓ depois de uma iteração
sem exceção: a fila inalcançável de forma persistente (rede, credencial, permissão) deixa o heartbeat envelhecer, o
healthcheck reprova e o ECS recicla a task. Mensagem que falha (registro inválido, banco fora) não conta: fica
para reentrega e DLQ, e o lote segue, como a órfã que FALHOU no reconciliador.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from limites.adapters.inbound.legado.traducao import traduzir
from limites.application.casos_de_uso import SincronizarComLegado
from limites.config import Configuracao
from limites.domain.legado import ResultadoDaSincronizacao
from limites.main import cliente_aws, construir_sincronizacao

log = logging.getLogger("limites.acl")
log.setLevel(logging.INFO)

HEARTBEAT = Path("/tmp/limites-acl.heartbeat")
ESPERA_S = 10
MAX_MENSAGENS = 10


def processar_registro(
    registro: str, caso_de_uso: SincronizarComLegado
) -> ResultadoDaSincronizacao | None:
    """None = tipo de registro que não interessa a este BC (ignorado e confirmado)."""
    atualizacao = traduzir(registro)
    if atualizacao is None:
        return None
    return caso_de_uso.executar(atualizacao)


# --- AWS: Lambda com event source mapping de SQS (ReportBatchItemFailures) -----------------------

_caso_de_uso: SincronizarComLegado | None = None


def caso_de_uso() -> SincronizarComLegado:
    """Clientes boto3 criados uma vez por container (lazy, para importar sem ambiente)."""
    global _caso_de_uso  # noqa: PLW0603
    if _caso_de_uso is None:
        _caso_de_uso = construir_sincronizacao(Configuracao.do_ambiente())
    return _caso_de_uso


def handler(evento: dict[str, Any], contexto: object) -> dict[str, list[dict[str, str]]]:
    falhas: list[dict[str, str]] = []
    desfechos: Counter[str] = Counter()
    for mensagem in evento.get("Records", []):
        try:
            resultado = processar_registro(mensagem["body"], caso_de_uso())
            desfechos[resultado.value if resultado else "IGNORADO"] += 1
        except Exception:
            # Só ESTE item volta para a fila (e, esgotadas as tentativas, para a DLQ).
            log.exception("falha ao sincronizar a mensagem %s", mensagem.get("messageId"))
            desfechos["FALHA"] += 1
            falhas.append({"itemIdentifier": mensagem["messageId"]})
    log.info(json.dumps({"msg": "lote", **desfechos}))
    return {"batchItemFailures": falhas}


# --- Local: long polling da fila na LocalStack ---------------------------------------------------


def uma_iteracao(
    sqs: Any,
    url_fila: str,
    caso: SincronizarComLegado,
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
            resultado = processar_registro(mensagem["Body"], caso)
        except Exception:
            log.exception("falha ao sincronizar %s; fica para reentrega", mensagem.get("MessageId"))
            desfechos["FALHA"] += 1
            continue
        sqs.delete_message(QueueUrl=url_fila, ReceiptHandle=mensagem["ReceiptHandle"])
        desfechos[resultado.value if resultado else "IGNORADO"] += 1
    if desfechos:
        log.info(json.dumps({"msg": "lote", **desfechos}))
    return desfechos


def iterar_e_sinalizar(
    sqs: Any, url_fila: str, caso: SincronizarComLegado, heartbeat: Path
) -> bool:
    """Uma iteração; renova o heartbeat só se ela terminou sem exceção. Nunca levanta (o loop continua)."""
    try:
        uma_iteracao(sqs, url_fila, caso)
    except Exception:
        log.exception("iteração falhou; heartbeat não renovado")
        return False
    heartbeat.touch()
    return True


def _esperar_fila(sqs: Any, nome: str, tentativas: int = 30) -> str:
    """A fila nasce no bootstrap, que pode terminar depois do container subir."""
    for tentativa in range(1, tentativas + 1):
        try:
            return str(sqs.get_queue_url(QueueName=nome)["QueueUrl"])
        except sqs.exceptions.QueueDoesNotExist:
            log.warning("fila %s ainda não existe (%d/%d)", nome, tentativa, tentativas)
            # Esperar a fila não é estar doente, e a espera tem fim: sem fila, o processo sai.
            HEARTBEAT.touch()
            time.sleep(2)
    sys.exit(f"fila {nome!r} não apareceu; rode o bootstrap")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = Configuracao.do_ambiente()
    # Local: endpoint obrigatório (e validado como local em `cliente_aws`). Na AWS (serviço ECS limites-acl)
    # não há endpoint; sem opt-in explícito o loop recusa subir, para nunca usar credenciais reais por engano.
    if config.endpoint_aws is None and os.environ.get("ACL_MODO_AWS") != "true":
        sys.exit("AWS_ENDPOINT_URL é obrigatório (modo local); na AWS defina ACL_MODO_AWS=true")
    try:
        sqs = cliente_aws("sqs", config)
        caso = construir_sincronizacao(config)
    except ValueError as erro:
        sys.exit(f"recusado: {erro}")
    url_fila = _esperar_fila(sqs, config.fila_legado)
    log.info("ACL do legado iniciada: fila=%s tabela=%s", config.fila_legado, config.tabela_limites)
    while True:
        if not iterar_e_sinalizar(sqs, url_fila, caso, HEARTBEAT):
            time.sleep(1)


if __name__ == "__main__":
    main()
