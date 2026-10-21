"""Reconciliador de órfãs: autorizações em curso cujo processo morreu e o cliente não retomou.

Roda como serviço ECS com 1 task (loop): na AWS precisa do ECS Service Connect para chamar o
limites, que Lambda não alcança (ADR-0004); na demo local, como container. Para cada órfã com o lease
vencido há mais que a GRACA, ASSUME o lease (CAS) — se perder, alguém (o próprio cliente) retomou — e:

  - RESERVA / DEBITO → COMPENSA, uma máquina de estados gravada passo a passo (ADR-0006):
      0. grava COMPENSANDO: daqui em diante o cliente não retoma a chave (não há disputa com ele);
      1. PERGUNTA ao limite, numa leitura consistente que não escreve nada, se a operação está aplicada:
         DEBITO → o id está na guarda? RESERVA → o hold com esse id está ativo?
         Não está → nada a desfazer: COMPENSADA. Está → grava `aplicacao_confirmada_em`;
      2. grava `desfazer_desde` e DESFAZ com id derivado (ESTORNAR_DEBITO / LIBERAR_RESERVA) → COMPENSADA.
    Nunca se aplica nada para "descobrir" o estado (a 1ª versão aplicava a operação antes de desfazer e
    podia deixar um débito fantasma se a reversão fosse recusada). Quando não dá para decidir com segurança,
    a órfã vai para RECONCILIACAO_MANUAL com motivo, progresso e evento.
  - CREDITO / ESTORNO / captura → CONCLUI (roll forward): recompor limite, desfazer e liquidar um hold já
    aprovado não aumentam a exposição; terminar é mais seguro que desfazer.

Por que a leitura decide. Uma escrita da operação original só cai até alguns segundos depois do envio: o
limites recusa começar a aplicar se o prazo do chamador (`X-Prazo-Ms`, ~2 s por tentativa) já venceu na fila
dele, e a escrita que começou termina dentro do orçamento de timeouts e retries do cliente DynamoDB do limites
(pior caso ~38 s, ADR-0006). O reconciliador só age com o lease vencido há mais de 60 s. Então, quando ele
pergunta, nada mais vai chegar. DEBITO depende da guarda (5 min): fora do PRAZO_DE_REAPLICACAO a resposta
"não está na guarda" não prova nada e o caso vai para a fila. RESERVA não depende: o hold é durável e só a
própria compensação o remove de uma autorização não aprovada.

Tempo: cada passo relê o relógio e renova o lease (10 s), e faz no máximo uma chamada ao limite (≤ ~4 s com
as duas tentativas). GRACA 60 s ≪ PRAZO_DE_REAPLICACAO 4 min < guarda do limite 5 min.

Uma órfã que falha de forma inesperada conta como FALHOU (com o id no log) e o lote segue: um item envenenado
não pode parar a reconciliação das outras nem o expirador que roda depois.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.portas import (
    ArmazenamentoIndisponivel,
    BuscaDeOrfas,
    ContratoInexistente,
    DesfechoNoLimite,
    EmAndamento,
    GatewayDeLimites,
    LeasePerdido,
    LimitesIndisponivel,
    OperacaoNoLimite,
    RegistroDeCompensacao,
    Relogio,
    RepositorioDeAutorizacoes,
    TipoNoLimite,
)
from autorizacao.domain.autorizacao import (
    Autorizacao,
    MotivoDaReconciliacao,
    StatusAutorizacao,
)
from autorizacao.domain.pedido import TipoOperacao

_log = logging.getLogger(__name__)

GRACA = timedelta(seconds=60)
NAMESPACE_COMPENSACOES = uuid.UUID("e7f6a5b4-c3d2-4e1f-9a8b-7c6d5e4f3a2b")


def id_operacao_de_compensacao(id_autorizacao: str) -> str:
    return str(uuid.uuid5(NAMESPACE_COMPENSACOES, id_autorizacao))


class RepositorioReconciliavel(
    RepositorioDeAutorizacoes, BuscaDeOrfas, RegistroDeCompensacao, Protocol
):
    """O reconciliador precisa das três portas; os casos de uso síncronos só da primeira (ISP)."""


class ResultadoDaReconciliacao(StrEnum):
    COMPENSADA = "COMPENSADA"
    CONCLUIDA = "CONCLUIDA"
    ENCAMINHADA = "ENCAMINHADA"  # decisão humana: fila RM# + alarme
    IGNORADA = "IGNORADA"  # dentro da graça, ou outra execução assumiu primeiro
    FALHOU = "FALHOU"  # Limites/banco fora ou falha inesperada: continua em curso e volta na próxima rodada


@dataclass(frozen=True, slots=True)
class ReconciliarOrfas:
    repositorio: RepositorioReconciliavel
    limites: GatewayDeLimites
    relogio: Relogio
    autorizar: AutorizarTransacao

    def executar(self, limite: int = 100) -> Counter[ResultadoDaReconciliacao]:
        contagem: Counter[ResultadoDaReconciliacao] = Counter()
        for orfa in self.repositorio.listar_orfas(self.relogio.agora() - GRACA, limite):
            try:
                resultado = self.reconciliar(orfa)
            except (
                ArmazenamentoIndisponivel
            ):  # inclusive no `assumir`, antes do try de `reconciliar`
                resultado = ResultadoDaReconciliacao.FALHOU
            except Exception:
                _log.exception(
                    "órfã %s: falha inesperada; fica para a próxima rodada", orfa.id_autorizacao
                )
                resultado = ResultadoDaReconciliacao.FALHOU
            contagem[resultado] += 1
        return contagem

    def reconciliar(self, orfa: Autorizacao) -> ResultadoDaReconciliacao:
        agora = self.relogio.agora()
        if orfa.lease_ate > agora - GRACA:
            return ResultadoDaReconciliacao.IGNORADA
        assumida = orfa.assumir(agora)
        if not self.repositorio.assumir(assumida, orfa.lease_ate):
            return ResultadoDaReconciliacao.IGNORADA
        try:
            if orfa.tipo in (TipoOperacao.CREDITO, TipoOperacao.ESTORNO) or orfa.eh_captura:
                if not assumida.pode_reaplicar(agora):
                    return self._encaminhar(assumida, MotivoDaReconciliacao.FORA_DO_PRAZO)
                return self._concluir(assumida)
            return self._compensar(assumida)
        except (LimitesIndisponivel, ArmazenamentoIndisponivel, LeasePerdido, EmAndamento):
            return ResultadoDaReconciliacao.FALHOU

    def _concluir(self, assumida: Autorizacao) -> ResultadoDaReconciliacao:
        original = None
        if assumida.id_autorizacao_original is not None:
            original = self.repositorio.obter(assumida.id_autorizacao_original)
            if original is None:
                return (
                    ResultadoDaReconciliacao.FALHOU
                )  # inconsistência: exige análise humana (alarme)
        self.autorizar.processar(assumida, original)
        return ResultadoDaReconciliacao.CONCLUIDA

    def _compensar(self, a: Autorizacao) -> ResultadoDaReconciliacao:
        reserva = (
            a.tipo is TipoOperacao.RESERVA
        )  # o hold é durável: a pergunta vale a qualquer tempo
        if a.status is StatusAutorizacao.EM_PROCESSAMENTO:
            if not reserva and not a.pode_reaplicar(self.relogio.agora()):
                return self._encaminhar(a, MotivoDaReconciliacao.FORA_DO_PRAZO)
            a = self._gravar(a, a.iniciar_compensacao(self.relogio.agora()))
        if a.aplicacao_confirmada_em is None:
            if not reserva and not a.pode_reaplicar(self.relogio.agora()):
                return self._encaminhar(a, MotivoDaReconciliacao.FORA_DO_PRAZO)
            try:
                estado = self.limites.consultar_operacao(a.id_contrato, a.id_autorizacao)
            except ContratoInexistente:
                return self._compensada(a)  # contrato que não existe não tem efeito a desfazer
            if not (estado.reserva_ativa if reserva else estado.na_guarda):
                return self._compensada(a)  # o limite confirmou que a operação não está aplicada
            a = self._gravar(a, a.confirmar_aplicacao(self.relogio.agora()))
        if not reserva and not a.pode_desfazer(self.relogio.agora()):
            return self._encaminhar(a, MotivoDaReconciliacao.DESFAZER_FORA_DO_PRAZO)
        a = self._gravar(
            a, a.marcar_desfazer(self.relogio.agora())
        )  # 1ª tentativa registrada antes
        reversao = self.limites.aplicar(
            a.id_contrato,
            OperacaoNoLimite(
                TipoNoLimite.LIBERAR_RESERVA if reserva else TipoNoLimite.ESTORNAR_DEBITO,
                id_operacao_de_compensacao(a.id_autorizacao),
                a.valor,
                a.id_autorizacao if reserva else None,
            ),  # sem id_conta: desfazer não pode ser barrado por conta que mudou depois (portabilidade)
        )
        if reversao.desfecho is DesfechoNoLimite.RECUSADA:
            if reserva and reversao.motivo == "RESERVA_NAO_ENCONTRADA":
                # Hold de autorização não aprovada só sai por esta compensação: já foi liberado.
                return self._compensada(a)
            return self._encaminhar(a, MotivoDaReconciliacao.DESFAZER_RECUSADO)
        return self._compensada(a)

    def _gravar(self, atual: Autorizacao, nova: Autorizacao) -> Autorizacao:
        if not self.repositorio.regravar(nova, atual.lease_ate):
            raise LeasePerdido(
                atual.id_autorizacao
            )  # outra execução assumiu: aborta sem chamar o limite
        return nova

    def _compensada(self, a: Autorizacao) -> ResultadoDaReconciliacao:
        compensada = a.compensar(self.relogio.agora(), self.autorizar.ids.novo_id())
        self.repositorio.concluir(compensada, None)
        return ResultadoDaReconciliacao.COMPENSADA

    def _encaminhar(
        self, a: Autorizacao, motivo: MotivoDaReconciliacao
    ) -> ResultadoDaReconciliacao:
        encaminhada = a.encaminhar_para_reconciliacao(
            motivo, self.relogio.agora(), self.autorizar.ids.novo_id()
        )
        self.repositorio.concluir(encaminhada, None)
        return ResultadoDaReconciliacao.ENCAMINHADA
