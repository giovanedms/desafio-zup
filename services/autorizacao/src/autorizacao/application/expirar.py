"""Expirador de reservas: devolve ao limite os holds aprovados que ninguém capturou nem estornou em 7 dias.

Roda no mesmo processo do reconciliador (precisa do BC Limites pelo Service Connect, como ele). Para cada
RESERVA vencida do índice esparso:
  1. LIBERA o hold no limite com um id DERIVADO da reserva (o expirador que cair no meio repete a MESMA
     operação, e a guarda do limite a absorve);
  2. grava a reserva EXPIRADA + `ReservaExpirada` no outbox, condicionada a ela ainda estar APROVADA.

Corrida com captura/estorno do mesmo hold: o BC Limites é o árbitro. Quem remove o hold vence; os outros
recebem RESERVA_NAO_ENCONTRADA. Se o expirador perde, só segue em frente: o vencedor é quem atualiza a
reserva original, em segundos. Se a reserva continua APROVADA e sem hold 10 min depois do vencimento, o
vencedor provavelmente não vai aparecer (expiração interrompida por mais que a guarda, ou captura/estorno em
reconciliação manual): a reserva passa a RECONCILIACAO_MANUAL (sai do índice de vencimentos, entra na fila
humana, com evento), em vez de sangrar uma chamada ao limite a cada rodada. Se o vencedor ainda estava
concluindo (expirador atrasado chegou no meio), ele encerra a reserva assim mesmo e ela sai da fila
(`Autorizacao.encerravel_pelo_vencedor`).

Como no reconciliador, uma reserva que falha de forma inesperada conta como FALHOU (id no log) e o lote segue.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from autorizacao.application.portas import (
    ArmazenamentoIndisponivel,
    ContratoInexistente,
    DesfechoNoLimite,
    ExpiracaoDeReservas,
    GatewayDeLimites,
    GeradorDeIds,
    LimitesIndisponivel,
    OperacaoNoLimite,
    OriginalJaEncerrada,
    Relogio,
    TipoNoLimite,
)
from autorizacao.domain.autorizacao import Autorizacao
from autorizacao.domain.idempotencia import id_operacao_de_expiracao

_log = logging.getLogger(__name__)

# Disputa normal pelo hold se resolve em segundos; o vencedor mais lento (captura/estorno órfão) se resolve
# em até ~4 min (prazo de reaplicação) ou vai para a fila humana. 10 min sem vencedor é anomalia.
TOLERANCIA_DA_DISPUTA = timedelta(minutes=10)


class ResultadoDaExpiracao(StrEnum):
    EXPIRADA = "EXPIRADA"
    PERDEU_O_HOLD = "PERDEU_O_HOLD"  # captura/estorno levou o hold antes (normal em corrida)
    ENCAMINHADA = "ENCAMINHADA"  # sem hold e sem vencedor há mais que a tolerância: fila humana
    IGNORADA = "IGNORADA"  # índice atrasado, ainda não venceu, ou outro expirador gravou primeiro
    FALHOU = "FALHOU"  # Limites/banco fora, contrato sumiu, hold divergente ou falha inesperada: + alarme


@dataclass(frozen=True, slots=True)
class ExpirarReservas:
    repositorio: ExpiracaoDeReservas
    limites: GatewayDeLimites
    relogio: Relogio
    ids: GeradorDeIds

    def executar(self, limite: int = 100) -> Counter[ResultadoDaExpiracao]:
        contagem: Counter[ResultadoDaExpiracao] = Counter()
        for reserva in self.repositorio.listar_reservas_vencidas(self.relogio.agora(), limite):
            try:
                resultado = self.expirar(reserva)
            except ArmazenamentoIndisponivel:
                resultado = ResultadoDaExpiracao.FALHOU
            except Exception:
                _log.exception(
                    "reserva %s: falha inesperada; fica para a próxima rodada",
                    reserva.id_autorizacao,
                )
                resultado = ResultadoDaExpiracao.FALHOU
            contagem[resultado] += 1
        return contagem

    def expirar(self, reserva: Autorizacao) -> ResultadoDaExpiracao:
        agora = self.relogio.agora()
        if not reserva.reserva_pendente or reserva.expira_em is None or reserva.expira_em > agora:
            return ResultadoDaExpiracao.IGNORADA
        liberacao = OperacaoNoLimite(
            TipoNoLimite.LIBERAR_RESERVA,
            id_operacao_de_expiracao(reserva.id_autorizacao),
            reserva.valor,
            reserva.id_autorizacao,
        )  # sem id_conta: a conta pode ter mudado em 7 dias (portabilidade) e o hold não pode ficar preso
        try:
            resultado = self.limites.aplicar(reserva.id_contrato, liberacao)
        except (LimitesIndisponivel, ContratoInexistente):
            return ResultadoDaExpiracao.FALHOU
        if resultado.desfecho is DesfechoNoLimite.RECUSADA:
            if resultado.motivo == "RESERVA_NAO_ENCONTRADA":
                if agora - reserva.expira_em > TOLERANCIA_DA_DISPUTA:
                    try:
                        self.repositorio.encaminhar_reserva(
                            reserva.encaminhar_reserva_sem_hold(agora, self.ids.novo_id())
                        )
                    except OriginalJaEncerrada:
                        return ResultadoDaExpiracao.IGNORADA  # o vencedor encerrou nesse meio-tempo
                    return ResultadoDaExpiracao.ENCAMINHADA
                return ResultadoDaExpiracao.PERDEU_O_HOLD
            return ResultadoDaExpiracao.FALHOU  # RESERVA_DIVERGENTE: valor gravado ≠ do hold (bug)
        expirada = reserva.expirar(resultado.saldos, agora, self.ids.novo_id())
        try:
            self.repositorio.registrar_expiracao(expirada)
        except OriginalJaEncerrada:
            # Outra task do expirador, com a MESMA liberação (id derivado), gravou primeiro.
            return ResultadoDaExpiracao.IGNORADA
        return ResultadoDaExpiracao.EXPIRADA
