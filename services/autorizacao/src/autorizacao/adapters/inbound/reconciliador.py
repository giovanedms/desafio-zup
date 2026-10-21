"""Loop do reconciliador de órfãs e do expirador de reservas: `python -m autorizacao.adapters.inbound.reconciliador`.

A cada `INTERVALO_RECONCILIADOR_S` (default 30) chama `ReconciliarOrfas.executar()` e
`ExpirarReservas.executar()` e loga a contagem por resultado (uma linha JSON por rodada, base dos alarmes
de ENCAMINHADA e FALHOU). As duas varreduras são independentes: uma que levanta não impede a outra.
Heartbeat em /tmp/reconciliador.heartbeat (healthcheck dos containers), renovado SÓ depois de uma rodada
sem exceção: falha persistente deixa o heartbeat envelhecer, o healthcheck reprova e o ECS recicla a task.

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
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from types import FrameType

from autorizacao.application.expirar import ExpirarReservas
from autorizacao.application.reconciliar import ReconciliarOrfas
from autorizacao.config import Configuracao
from autorizacao.main import construir_reconciliador

log = logging.getLogger("autorizacao.reconciliador")

HEARTBEAT = Path("/tmp/reconciliador.heartbeat")


class RodadaIncompleta(Exception):
    """Uma das varreduras levantou (a outra rodou e a contagem foi logada): o heartbeat não é renovado."""


def _varredura[R: StrEnum](
    nome: str, prefixo: str, executar: Callable[[], Counter[R]]
) -> dict[str, int] | None:
    """Contagem `<prefixo>_<RESULTADO>` de uma varredura; None (com o traceback no log) se ela levantar."""
    try:
        return {f"{prefixo}_{r.value}": n for r, n in executar().items()}
    except Exception:
        log.exception("varredura de %s falhou", nome)
        return None


def uma_rodada(reconciliador: ReconciliarOrfas, expirador: ExpirarReservas) -> Counter[str]:
    """Uma varredura de órfãs e uma de reservas vencidas, independentes; devolve (e loga) a contagem por
    resultado. Levanta `RodadaIncompleta` depois de logar, se alguma das duas falhou."""
    orfas = _varredura("órfãs", "orfa", reconciliador.executar)
    reservas = _varredura("reservas vencidas", "reserva", expirador.executar)
    contagem: Counter[str] = Counter({**(orfas or {}), **(reservas or {})})
    falharam = [nome for nome, parte in (("orfas", orfas), ("reservas", reservas)) if parte is None]
    log.info(
        json.dumps(
            {
                "msg": "rodada",
                "orfas": sum((orfas or {}).values()),
                "reservas_vencidas": sum((reservas or {}).values()),
                **contagem,
                **({"varreduras_com_erro": falharam} if falharam else {}),
            }
        )
    )
    if falharam:
        raise RodadaIncompleta(", ".join(falharam))
    return contagem


def rodar_e_sinalizar(
    reconciliador: ReconciliarOrfas, expirador: ExpirarReservas, heartbeat: Path = HEARTBEAT
) -> bool:
    """Uma rodada; renova o heartbeat só se ela terminou sem exceção. Nunca levanta (o loop continua)."""
    try:
        uma_rodada(reconciliador, expirador)
    except RodadaIncompleta as erro:
        log.error("rodada incompleta (%s); heartbeat não renovado", erro)
        return False
    except Exception:
        log.exception("rodada falhou; heartbeat não renovado")
        return False
    heartbeat.touch()
    return True


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
        rodar_e_sinalizar(reconciliador, expirador)
        parar.wait(config.intervalo_reconciliador_s)
    log.info("reconciliador encerrado")
    sys.exit(0)


if __name__ == "__main__":
    main()
