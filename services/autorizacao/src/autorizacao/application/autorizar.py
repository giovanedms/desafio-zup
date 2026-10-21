"""Caso de uso AutorizarTransacao — o fluxo síncrono do POST /v1/contratos/{id}/autorizacoes.

Responsabilidade única (SRP): este módulo muda quando muda o fluxo de decisão de uma autorização (a
ordem abaixo, as regras da original), não quando muda o transporte HTTP, o banco ou o formato do evento.

Ordem (ADR-0006):
  1. Idempotência primeiro: se o id determinístico já existe → replay / 409 / 422 / retomada. Sem original
     (o caso comum), o próprio PutItem condicional do passo 3 é a consulta: devolve a gravada se já existir,
     e o caminho quente economiza uma leitura.
  2. Estorno ou captura (DEBITO com id_autorizacao_original): valida a original antes de persistir.
  3. Registra EM_PROCESSAMENTO com lease (escrita condicional; corrida → volta ao passo 1).
  4. Aplica a operação no BC Limites (idempotente pelo id da operação).
  5. Conclui: estado final + eventos no outbox numa escrita atômica, condicionada ao lease.
Se o Limites estiver indisponível, a autorização fica EM_PROCESSAMENTO: o retry do cliente com a
mesma chave retoma depois do lease (até o PRAZO_DE_REAPLICACAO); sem retry, o reconciliador resolve.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from autorizacao.application.portas import (
    AutorizacaoOriginalInvalida,
    ChaveReutilizada,
    ConflitoDeEstado,
    ContratoInexistente,
    DesfechoNoLimite,
    EmAndamento,
    EmReconciliacao,
    EncerradaSemResultado,
    GatewayDeLimites,
    GeradorDeIds,
    LeasePerdido,
    OperacaoNoLimite,
    OriginalJaEncerrada,
    Relogio,
    RepositorioDeAutorizacoes,
    TipoNoLimite,
)
from autorizacao.domain.autorizacao import Autorizacao, MotivoRecusa, StatusAutorizacao
from autorizacao.domain.idempotencia import (
    EscopoDeIdempotencia,
    Reexecucao,
    avaliar_reexecucao,
    id_operacao_de_estorno,
    impressao_digital,
)
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

_log = logging.getLogger(__name__)
_MOTIVOS_DO_LIMITE = {m.value: m for m in MotivoRecusa}
_ESTORNAVEIS = (TipoOperacao.RESERVA, TipoOperacao.DEBITO)
_JA_ENCERRADAS = (
    StatusAutorizacao.ESTORNADA,
    StatusAutorizacao.EXPIRADA,
    StatusAutorizacao.CAPTURADA,
    StatusAutorizacao.RECONCILIACAO_MANUAL,  # reserva sem hold em análise: ninguém mexe até decidirem
)
# Estorno dono da marca que ainda pode aplicar no limite: bloqueia outro estorno da mesma original.
_ESTORNO_EM_VOO = (StatusAutorizacao.EM_PROCESSAMENTO, StatusAutorizacao.RECONCILIACAO_MANUAL)


@dataclass(frozen=True, slots=True)
class Contexto:
    correlation_id: str
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class Desfecho:
    """O que o adapter HTTP precisa para responder: a autorização e se é um replay."""

    autorizacao: Autorizacao
    replay: bool


@dataclass(frozen=True, slots=True)
class AutorizarTransacao:
    repositorio: RepositorioDeAutorizacoes
    limites: GatewayDeLimites
    relogio: Relogio
    ids: GeradorDeIds

    def executar(
        self, pedido: PedidoDeAutorizacao, escopo: EscopoDeIdempotencia, contexto: Contexto
    ) -> Desfecho:
        impressao = impressao_digital(pedido)
        id_autorizacao = escopo.id_autorizacao()

        if pedido.id_autorizacao_original is not None:
            # Estorno/captura: o replay precisa vir ANTES da validação da original (que o próprio
            # pedido pode ter encerrado), então aqui a leitura é explícita.
            existente = self.repositorio.obter(id_autorizacao)
            if existente is not None:
                return self._reexecutar(existente, impressao, pedido)

        original = self._validar_original(pedido, escopo.client_id)
        nova = Autorizacao.iniciar(
            pedido,
            id_autorizacao=id_autorizacao,
            impressao_digital=impressao,
            client_id=escopo.client_id,
            agora=self.relogio.agora(),
            correlation_id=contexto.correlation_id,
            traceparent=contexto.traceparent,
        )
        concorrente = self.repositorio.registrar(nova)
        if concorrente is not None:
            return self._reexecutar(concorrente, impressao, pedido)
        return self.processar(nova, original)

    # --- passos -------------------------------------------------------------------------------

    def _reexecutar(
        self, existente: Autorizacao, impressao: str, pedido: PedidoDeAutorizacao
    ) -> Desfecho:
        agora = self.relogio.agora()
        match avaliar_reexecucao(existente, impressao, agora):
            case Reexecucao.REPLAY:
                return Desfecho(existente, replay=True)
            case Reexecucao.CHAVE_REUTILIZADA:
                raise ChaveReutilizada(existente.id_autorizacao)
            case Reexecucao.EM_ANDAMENTO:
                raise EmAndamento(int((existente.lease_ate - agora).total_seconds()) + 1)
            case Reexecucao.ENCERRADA_SEM_RESULTADO:
                raise EncerradaSemResultado(existente.id_autorizacao)
            case Reexecucao.EM_RECONCILIACAO:
                raise EmReconciliacao(existente.id_autorizacao)
            case Reexecucao.RETOMAR:
                assumida = existente.assumir(agora)
                if not self.repositorio.assumir(assumida, existente.lease_ate):
                    raise EmAndamento(1)
                return self.processar(assumida, self._validar_original(pedido, existente.client_id))

    def _validar_original(self, pedido: PedidoDeAutorizacao, client_id: str) -> Autorizacao | None:
        """Estorno: original estornável, aprovada e de mesmo valor. Captura: RESERVA aprovada que cobre
        o valor. Em ambos, original já encerrada (estornada/capturada/expirada) → 409. Original de OUTRO
        canal é tratada como inexistente: saber o UUID não dá direito de estornar nem capturar (BOLA)."""
        if pedido.id_autorizacao_original is None:
            return None
        acao = "estornável" if pedido.tipo is TipoOperacao.ESTORNO else "capturável"
        original = self.repositorio.obter(pedido.id_autorizacao_original)
        if (
            original is None
            or original.id_contrato != pedido.id_contrato
            or original.client_id != client_id
        ):
            raise AutorizacaoOriginalInvalida("autorização original inexistente neste contrato")
        aceitos = _ESTORNAVEIS if pedido.tipo is TipoOperacao.ESTORNO else (TipoOperacao.RESERVA,)
        if original.tipo not in aceitos:
            raise AutorizacaoOriginalInvalida(f"{original.tipo} não é {acao}")
        if original.status in _JA_ENCERRADAS:
            raise ConflitoDeEstado(f"autorização original já está {original.status}")
        if original.status is not StatusAutorizacao.APROVADA:
            raise AutorizacaoOriginalInvalida(f"só autorizações aprovadas são {acao}s")
        if pedido.tipo is TipoOperacao.ESTORNO and original.valor != pedido.valor:
            raise AutorizacaoOriginalInvalida("estorno parcial não é suportado na v1")
        if pedido.eh_captura and pedido.valor > original.valor:
            raise AutorizacaoOriginalInvalida("captura maior que o valor reservado")
        return original

    def processar(self, autorizacao: Autorizacao, original: Autorizacao | None) -> Desfecho:
        """Aplica no limite e conclui. Público: o reconciliador reusa para concluir órfãs (roll forward)."""
        if autorizacao.tipo is TipoOperacao.ESTORNO and original is not None:
            impedimento = self._marcar_estorno(original, autorizacao)
            if impedimento is not None:
                recusada = autorizacao.recusar(
                    impedimento, None, self.relogio.agora(), self.ids.novo_id()
                )
                return self._concluir(autorizacao, recusada, None)
        try:
            resultado = self.limites.aplicar(
                autorizacao.id_contrato, _operacao_no_limite(autorizacao, original)
            )
        except ContratoInexistente:
            recusada = autorizacao.recusar(
                MotivoRecusa.CONTRATO_NAO_ENCONTRADO, None, self.relogio.agora(), self.ids.novo_id()
            )
            return self._concluir(autorizacao, recusada, None)
        # LimitesIndisponivel propaga: a autorização fica EM_PROCESSAMENTO e é retomável.

        agora = self.relogio.agora()
        if resultado.desfecho in (DesfechoNoLimite.APLICADA, DesfechoNoLimite.JA_APLICADA):
            aprovada = autorizacao.aprovar(resultado.saldos, agora, self.ids.novo_id(), original)
            return self._concluir(autorizacao, aprovada, _encerrar(autorizacao, original, agora))
        # Motivo que este BC ainda não conhece (o limites evoluiu antes): recusa genérica, nunca 500, e uma
        # linha JSON com o valor cru (metric filter + alarme: a degradação não fica silenciosa).
        motivo = _MOTIVOS_DO_LIMITE.get(resultado.motivo or "")
        if motivo is None:
            _log.warning(
                json.dumps({"evento": "motivo_desconhecido_do_limite", "motivo": resultado.motivo})
            )
            motivo = MotivoRecusa.RECUSA_DO_LIMITE
        # Conta que não é a titular: o canal não pode ver o saldo de um contrato alheio (nem no GET, que mostra
        # os saldos gravados, nem no TransacaoRecusada). A recusa fica registrada sem saldos.
        saldos = None if motivo is MotivoRecusa.CONTA_DIVERGENTE else resultado.saldos
        recusada = autorizacao.recusar(motivo, saldos, agora, self.ids.novo_id())
        return self._concluir(autorizacao, recusada, None)

    def _marcar_estorno(self, original: Autorizacao, estorno: Autorizacao) -> MotivoRecusa | None:
        """Um estorno em voo por original. Marca velha (dono recusado/compensado) é assumida por CAS."""
        dono = self.repositorio.marcar_estorno(original.id_autorizacao, estorno.id_autorizacao)
        if dono:
            outro = self.repositorio.obter(dono)
            # Dono desconhecido (o TTL apagou o item) não prova que ele terminou: fail-closed.
            if outro is None or outro.status in _ESTORNO_EM_VOO:
                return MotivoRecusa.ESTORNO_EM_ANDAMENTO
            # Marca velha: assume no lugar dela (se outro estorno assumir antes, o CAS devolve o novo dono).
            dono = self.repositorio.marcar_estorno(
                original.id_autorizacao, estorno.id_autorizacao, substituir=dono
            )
        if dono is None:
            return None
        return (
            MotivoRecusa.ORIGINAL_JA_ENCERRADA if dono == "" else MotivoRecusa.ESTORNO_EM_ANDAMENTO
        )

    def _concluir(
        self,
        em_processamento: Autorizacao,
        concluida: Autorizacao,
        original_encerrada: Autorizacao | None,
    ) -> Desfecho:
        try:
            self.repositorio.concluir(concluida, original_encerrada)
        except LeasePerdido:
            atual = self.repositorio.obter(concluida.id_autorizacao)
            if atual is not None and atual.resultado is not None:
                return Desfecho(atual, replay=True)  # outra execução concluiu a mesma operação
            raise EmAndamento(1) from None
        except OriginalJaEncerrada:
            # Outro estorno da mesma original venceu. O efeito no limite foi um só (id da operação
            # derivado da original), então esta autorização termina recusada, sem efeito próprio.
            # (Captura e expiração não chegam aqui: quem perde o hold já volta RESERVA_NAO_ENCONTRADA.)
            perdedora = em_processamento.recusar(
                MotivoRecusa.ORIGINAL_JA_ENCERRADA, None, self.relogio.agora(), self.ids.novo_id()
            )
            self.repositorio.concluir(perdedora, None)
            return Desfecho(perdedora.sem_eventos(), replay=False)
        return Desfecho(concluida.sem_eventos(), replay=False)


def _encerrar(
    autorizacao: Autorizacao, original: Autorizacao | None, agora: datetime
) -> Autorizacao | None:
    """Novo estado da original quando a autorização é aprovada (estorno → ESTORNADA, captura → CAPTURADA)."""
    if original is None:
        return None
    if autorizacao.eh_captura:
        return original.capturar(agora)
    return original.estornar(agora)


def _operacao_no_limite(autorizacao: Autorizacao, original: Autorizacao | None) -> OperacaoNoLimite:
    """Traduz o pedido para a linguagem do BC Limites (Customer/Supplier). Toda operação PEDIDA PELO CANAL leva o
    `id_conta` declarado: o limite o confere contra a titular do contrato (BOLA na escrita)."""
    valor, id_aut, conta = autorizacao.valor, autorizacao.id_autorizacao, autorizacao.id_conta
    match autorizacao.tipo:
        case TipoOperacao.RESERVA:
            return OperacaoNoLimite(TipoNoLimite.RESERVAR, id_aut, valor, id_conta=conta)
        case TipoOperacao.DEBITO if original is not None:
            # Captura: id PRÓPRIO (capturas diferentes do mesmo hold disputam; o limite decide quem leva).
            return OperacaoNoLimite(
                TipoNoLimite.CAPTURAR_RESERVA,
                id_aut,
                valor,
                original.id_autorizacao,
                valor_reservado=original.valor,
                id_conta=conta,
            )
        case TipoOperacao.DEBITO:
            return OperacaoNoLimite(TipoNoLimite.DEBITAR, id_aut, valor, id_conta=conta)
        case TipoOperacao.CREDITO:
            return OperacaoNoLimite(TipoNoLimite.CREDITAR, id_aut, valor, id_conta=conta)
        case TipoOperacao.ESTORNO:
            assert original is not None
            # Id derivado da ORIGINAL: estornos concorrentes da mesma autorização viram UMA operação.
            id_estorno = id_operacao_de_estorno(original.id_autorizacao)
            if original.tipo is TipoOperacao.RESERVA:
                return OperacaoNoLimite(
                    TipoNoLimite.LIBERAR_RESERVA,
                    id_estorno,
                    valor,
                    original.id_autorizacao,
                    id_conta=conta,
                )
            return OperacaoNoLimite(TipoNoLimite.ESTORNAR_DEBITO, id_estorno, valor, id_conta=conta)
