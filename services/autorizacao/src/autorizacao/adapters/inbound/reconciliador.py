"""Loop do reconciliador de órfãs e do expirador de reservas: `python -m autorizacao.adapters.inbound.reconciliador`.

A cada `INTERVALO_RECONCILIADOR_S` (default 30) chama `ReconciliarOrfas.executar()` e
`ExpirarReservas.executar()` e loga a contagem por resultado (uma linha JSON por rodada, base dos alarmes
de ENCAMINHADA e FALHOU). Heartbeat em /tmp/reconciliador.heartbeat (healthcheck dos containers).

NÃO é Lambda: na AWS o reconciliador precisa chamar o BC `limites` pelo ECS Service Connect
(http://limites:8080), que uma Lambda fora do namespace não alcança. Por isso roda como serviço ECS
(mesma imagem da autorizacao, cliente do Service Connect, uma task). Várias instâncias seriam seguras:
a posse da órfã é decidida pelo CAS do lease (`assumir`), não por eleição de líder.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from collections import Counter
from pathlib import Path
from types import FrameType

from autorizacao.application.expirar import ExpirarReservas
from autorizacao.application.reconciliar import ReconciliarOrfas
from autorizacao.config import Configuracao
from autorizacao.main import construir_reconciliador

log = logging.getLogger("autorizacao.reconciliador")

HEARTBEAT = Path("/tmp/reconciliador.heartbeat")


def uma_rodada(reconciliador: ReconciliarOrfas, expirador: ExpirarReservas) -> Counter[str]:
    """Uma varredura de órfãs e uma de reservas vencidas; devolve (e loga) a contagem por resultado."""
    orfas = {f"orfa_{r.value}": n for r, n in reconciliador.executar().items()}
    reservas = {f"reserva_{r.value}": n for r, n in expirador.executar().items()}
    contagem: Counter[str] = Counter({**orfas, **reservas})
    log.info(
        json.dumps(
            {
                "msg": "rodada",
                "orfas": sum(orfas.values()),
                "reservas_vencidas": sum(reservas.values()),
                **contagem,
            }
        )
    )
    return contagem


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = Configuracao.do_ambiente()
    reconciliador, expirador = construir_reconciliador(config)
    parar = threading.Event()

    def encerrar(_sinal: int, _quadro: FrameType | None) -> None:
        parar.set()  # SIGTERM do ECS/compose: termina a rodada em curso e sai

    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)
    log.info("reconciliador iniciado: intervalo=%.0fs", config.intervalo_reconciliador_s)
    while not parar.is_set():
        try:
            uma_rodada(reconciliador, expirador)
        except Exception:
            log.exception("rodada falhou; tentando de novo no próximo intervalo")
        HEARTBEAT.touch()
        parar.wait(config.intervalo_reconciliador_s)
    log.info("reconciliador encerrado")
    sys.exit(0)


if __name__ == "__main__":
    main()
