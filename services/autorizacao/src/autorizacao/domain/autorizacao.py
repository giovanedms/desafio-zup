"""Agregado Autorização: ciclo de vida de uma decisão sobre uma transação.

Estados:
    EM_PROCESSAMENTO ──aprovar──▶ APROVADA ──estornar──▶ ESTORNADA
          │                          ├──capturar (só RESERVA, por um DEBITO)──▶ CAPTURADA
          │                          └──expirar (só RESERVA)──▶ EXPIRADA
          ├──recusar──▶ RECUSADA
          ├──iniciar_compensacao (órfã, pelo reconciliador)──▶ COMPENSANDO ──compensar──▶ COMPENSADA
          │                                                       └──encaminhar──▶ RECONCILIACAO_MANUAL
          └──encaminhar (órfã que não dá para decidir com segurança)──▶ RECONCILIACAO_MANUAL
    APROVADA (RESERVA vencida, sem hold e sem vencedor) ──encaminhar_reserva_sem_hold──▶ RECONCILIACAO_MANUAL

COMPENSANDO é gravado ANTES de qualquer chamada ao limite: dali em diante o cliente não retoma mais a chave
(recebe "encerrada sem resultado"), então reconciliador e cliente nunca disputam o mesmo efeito. O progresso
(`aplicacao_confirmada_em`, `desfazer_desde`) também é gravado antes de cada passo, e a rodada seguinte
continua dele. Compensar e encaminhar registram evento: a trilha durável inclui os casos de efeito incerto.

Uma RESERVA aprovada sai de APROVADA por exatamente um caminho: captura, estorno ou expiração. Quem
decide qual é o BC Limites (a operação que remove o hold vence; as outras recebem RESERVA_NAO_ENCONTRADA).

`resultado` guarda o desfecho ORIGINAL (o que o replay devolve); `status` é o estado atual.
Uma aprovação estornada depois continua respondendo "aprovada" a um replay da chave original.
Cada transição relevante registra um evento de domínio em `eventos` (o repositório grava no outbox
na mesma escrita atômica do estado).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TypedDict

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
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

DURACAO_DO_LEASE = timedelta(seconds=10)
# Até quando é seguro (re)enviar a operação desta autorização ao BC Limites, contado da criação. A guarda
# de idempotência do limite dura 5 min (contrato limites-interno-v1); qualquer aplicação aconteceu depois
# de `criada_em`, então reenviar até criada_em + 4 min cai sempre dentro da guarda (1 min de folga para
# relógio e latência). Depois disso, reenviar poderia aplicar duas vezes: só reconciliação manual decide.
PRAZO_DE_REAPLICACAO = timedelta(minutes=4)
VALIDADE_DA_RESERVA = timedelta(days=7)
# Segundo evento de uma mesma transição (captura): id derivado do primeiro. Os dois ids mudam a cada tentativa
# (o primeiro vem do gerador), o que é inócuo: os dois eventos saem na MESMA transação atômica do concluir.
NAMESPACE_EVENTOS_DERIVADOS = uuid.UUID("3b0f2c1e-9d4a-5e6b-8c7d-1a2b3c4d5e6f")


class StatusAutorizacao(StrEnum):
    EM_PROCESSAMENTO = "EM_PROCESSAMENTO"
    APROVADA = "APROVADA"
    RECUSADA = "RECUSADA"
    ESTORNADA = "ESTORNADA"
    EXPIRADA = "EXPIRADA"
    CAPTURADA = "CAPTURADA"
    COMPENSANDO = "COMPENSANDO"
    COMPENSADA = "COMPENSADA"
    RECONCILIACAO_MANUAL = "RECONCILIACAO_MANUAL"


EM_CURSO = (StatusAutorizacao.EM_PROCESSAMENTO, StatusAutorizacao.COMPENSANDO)


class MotivoDaReconciliacao(StrEnum):
    """Por que a autorização saiu do caminho automático (o runbook começa por aqui)."""

    FORA_DO_PRAZO = (
        "FORA_DO_PRAZO"  # DEBITO/roll forward: a guarda já pode ter esquecido; perguntar não decide
    )
    DESFAZER_FORA_DO_PRAZO = (
        "DESFAZER_FORA_DO_PRAZO"  # aplicação confirmada; a reversão pode ter aplicado ou não
    )
    # aplicação confirmada; o limite recusou a reversão (ex.: pagamento entrou no meio)
    DESFAZER_RECUSADO = "DESFAZER_RECUSADO"
    RESERVA_SEM_HOLD = (
        "RESERVA_SEM_HOLD"  # reserva vencida APROVADA cujo hold sumiu (expiração interrompida)
    )


class Resultado(StrEnum):
    APROVADA = "APROVADA"
    RECUSADA = "RECUSADA"


class MotivoRecusa(StrEnum):
    LIMITE_INSUFICIENTE = "LIMITE_INSUFICIENTE"
    CONTRATO_INATIVO = "CONTRATO_INATIVO"
    CREDITO_EXCEDE_UTILIZADO = "CREDITO_EXCEDE_UTILIZADO"
    CONTRATO_NAO_ENCONTRADO = "CONTRATO_NAO_ENCONTRADO"  # registrado: o replay devolve o mesmo 404
    ORIGINAL_JA_ENCERRADA = "ORIGINAL_JA_ENCERRADA"  # estorno concorrente perdeu a corrida (409)
    RESERVA_DIVERGENTE = "RESERVA_DIVERGENTE"  # não deveria ocorrer: indica bug no chamador (409)
    RESERVA_NAO_ENCONTRADA = "RESERVA_NAO_ENCONTRADA"  # hold já capturado/estornado/expirado (409)
    ESTORNO_EM_ANDAMENTO = "ESTORNO_EM_ANDAMENTO"  # outro estorno da mesma original em voo (409)
    RECUSA_DO_LIMITE = (
        "RECUSA_DO_LIMITE"  # motivo novo do BC Limites que este BC ainda não mapeia (422)
    )


class _BaseDoEvento(TypedDict):
    id_evento: str
    ocorrido_em: datetime
    id_contrato: str
    correlation_id: str
    traceparent: str | None


class TransicaoInvalida(Exception):
    """Transição de estado não permitida pela máquina de estados."""


@dataclass(frozen=True, slots=True)
class Autorizacao:
    id_autorizacao: str
    id_contrato: str
    id_conta: str
    valor: Decimal
    moeda: str
    tipo: TipoOperacao
    id_estabelecimento: str | None
    id_autorizacao_original: str | None
    metadata: Mapping[str, object]
    impressao_digital: str
    client_id: str
    status: StatusAutorizacao
    criada_em: datetime
    atualizada_em: datetime
    lease_ate: datetime
    correlation_id: str
    traceparent: str | None = None
    resultado: Resultado | None = None
    motivo_recusa: MotivoRecusa | None = None
    saldos: SaldosDoLimite | None = None
    tipo_operacao_original: TipoOperacao | None = None
    # compensação: o limite confirmou (leitura) que a operação original está aplicada
    aplicacao_confirmada_em: datetime | None = None
    desfazer_desde: datetime | None = None  # compensação: 1ª tentativa da reversão (prazo próprio)
    motivo_da_reconciliacao: MotivoDaReconciliacao | None = None
    eventos: tuple[EventoDeDominio, ...] = field(default=())

    @classmethod
    def iniciar(
        cls,
        pedido: PedidoDeAutorizacao,
        *,
        id_autorizacao: str,
        impressao_digital: str,
        client_id: str,
        agora: datetime,
        correlation_id: str,
        traceparent: str | None,
    ) -> Autorizacao:
        return cls(
            id_autorizacao=id_autorizacao,
            id_contrato=pedido.id_contrato,
            id_conta=pedido.id_conta,
            valor=pedido.valor,
            moeda=pedido.moeda,
            tipo=pedido.tipo,
            id_estabelecimento=pedido.id_estabelecimento,
            id_autorizacao_original=pedido.id_autorizacao_original,
            metadata=MappingProxyType(dict(pedido.metadata)),
            impressao_digital=impressao_digital,
            client_id=client_id,
            status=StatusAutorizacao.EM_PROCESSAMENTO,
            criada_em=agora,
            atualizada_em=agora,
            lease_ate=agora + DURACAO_DO_LEASE,
            correlation_id=correlation_id,
            traceparent=traceparent,
        )

    @property
    def expira_em(self) -> datetime | None:
        """Só reservas aprovadas expiram."""
        if self.tipo is TipoOperacao.RESERVA and self.resultado is Resultado.APROVADA:
            return self.criada_em + VALIDADE_DA_RESERVA
        return None

    @property
    def reserva_pendente(self) -> bool:
        """Hold ainda vivo: aprovado e nem capturado, nem estornado, nem expirado."""
        return self.tipo is TipoOperacao.RESERVA and self.status is StatusAutorizacao.APROVADA

    @property
    def eh_captura(self) -> bool:
        return self.tipo is TipoOperacao.DEBITO and self.id_autorizacao_original is not None

    def pode_reaplicar(self, agora: datetime) -> bool:
        """Reenviar a operação ao limite ainda cai dentro da guarda de idempotência de lá?"""
        return agora - self.criada_em <= PRAZO_DE_REAPLICACAO

    def pode_desfazer(self, agora: datetime) -> bool:
        """Reenviar a REVERSÃO da compensação ainda cai dentro da guarda? (prazo contado da 1ª tentativa)"""
        return self.desfazer_desde is None or agora - self.desfazer_desde <= PRAZO_DE_REAPLICACAO

    def assumir(self, agora: datetime) -> Autorizacao:
        """Posse por CAS no lease: retomada pelo cliente (EM_PROCESSAMENTO) ou rodada do reconciliador."""
        self._exigir_um_de(EM_CURSO, "assumir")
        if self.lease_ate > agora:
            raise TransicaoInvalida("lease ainda válido: outra execução está em andamento")
        return replace(self, lease_ate=agora + DURACAO_DO_LEASE, atualizada_em=agora)

    def iniciar_compensacao(self, agora: datetime) -> Autorizacao:
        """O reconciliador decidiu desfazer: daqui em diante o cliente não retoma esta chave."""
        self._exigir(StatusAutorizacao.EM_PROCESSAMENTO, "iniciar a compensação")
        if self.tipo not in (TipoOperacao.RESERVA, TipoOperacao.DEBITO) or self.eh_captura:
            raise TransicaoInvalida(f"{self.tipo} conclui (roll forward), não compensa")
        return self._renovada(agora, status=StatusAutorizacao.COMPENSANDO)

    def confirmar_aplicacao(self, agora: datetime) -> Autorizacao:
        """O limite respondeu (leitura) que a operação ESTÁ aplicada: daqui em diante só falta desfazer."""
        self._exigir(StatusAutorizacao.COMPENSANDO, "confirmar a aplicação")
        return self._renovada(agora, aplicacao_confirmada_em=self.aplicacao_confirmada_em or agora)

    def marcar_desfazer(self, agora: datetime) -> Autorizacao:
        """Grava a 1ª tentativa da reversão ANTES de chamá-la (o prazo dela conta daqui)."""
        self._exigir(StatusAutorizacao.COMPENSANDO, "registrar a reversão")
        if self.aplicacao_confirmada_em is None:
            raise TransicaoInvalida("só desfaz o que o limite confirmou como aplicado")
        return self._renovada(agora, desfazer_desde=self.desfazer_desde or agora)

    def aprovar(
        self,
        saldos: SaldosDoLimite,
        agora: datetime,
        id_evento: str,
        original: Autorizacao | None = None,
    ) -> Autorizacao:
        self._exigir(StatusAutorizacao.EM_PROCESSAMENTO, "aprovar")
        evento: EventoDeDominio
        eventos: tuple[EventoDeDominio, ...] = ()
        if self.tipo is TipoOperacao.ESTORNO:
            if original is None:
                raise TransicaoInvalida("estorno aprovado precisa da autorização original")
            evento = TransacaoEstornada(
                **self._base_do_evento(id_evento, agora),
                id_autorizacao=self.id_autorizacao,
                id_autorizacao_original=original.id_autorizacao,
                tipo_operacao_original=original.tipo.value,
                valor=self.valor,
                saldos=saldos,
            )
        else:
            if self.eh_captura and original is None:
                raise TransicaoInvalida("captura aprovada precisa da reserva original")
            evento = TransacaoAutorizada(
                **self._base_do_evento(id_evento, agora),
                id_autorizacao=self.id_autorizacao,
                tipo_operacao=self.tipo.value,
                valor=self.valor,
                moeda=self.moeda,
                id_conta=self.id_conta,
                id_estabelecimento=self.id_estabelecimento,
                saldos=saldos,
                id_autorizacao_original=self.id_autorizacao_original,
            )
            if self.eh_captura and original is not None:
                id_derivado = str(uuid.uuid5(NAMESPACE_EVENTOS_DERIVADOS, id_evento))
                eventos = (
                    ReservaCapturada(
                        **self._base_do_evento(id_derivado, agora),
                        id_reserva=original.id_autorizacao,
                        id_autorizacao=self.id_autorizacao,
                        valor_reservado=original.valor,
                        valor_capturado=self.valor,
                        saldos=saldos,
                    ),
                )
        return replace(
            self,
            status=StatusAutorizacao.APROVADA,
            resultado=Resultado.APROVADA,
            saldos=saldos,
            tipo_operacao_original=original.tipo if original else None,
            atualizada_em=agora,
            eventos=(*self.eventos, evento, *eventos),
        )

    def recusar(
        self,
        motivo: MotivoRecusa,
        saldos: SaldosDoLimite | None,
        agora: datetime,
        id_evento: str,
    ) -> Autorizacao:
        """Recusa é fato de negócio: fica registrada e vira evento (antifraude, data platform)."""
        self._exigir(StatusAutorizacao.EM_PROCESSAMENTO, "recusar")
        evento = TransacaoRecusada(
            **self._base_do_evento(id_evento, agora),
            id_autorizacao=self.id_autorizacao,
            tipo_operacao=self.tipo.value,
            motivo=motivo.value,
            valor=self.valor,
            moeda=self.moeda,
            id_conta=self.id_conta,
            id_estabelecimento=self.id_estabelecimento,
            saldos=saldos,
        )
        return replace(
            self,
            status=StatusAutorizacao.RECUSADA,
            resultado=Resultado.RECUSADA,
            motivo_recusa=motivo,
            saldos=saldos,
            atualizada_em=agora,
            eventos=(*self.eventos, evento),
        )

    def compensar(self, agora: datetime, id_evento: str) -> Autorizacao:
        """Efeito líquido zero no limite (nada aplicado, ou aplicado e revertido). Quem compensa é o DONO
        DO LEASE: a conclusão é condicionada a ele na persistência."""
        self._exigir(StatusAutorizacao.COMPENSANDO, "compensar")
        evento = TransacaoCompensada(
            **self._base_do_evento(id_evento, agora),
            id_autorizacao=self.id_autorizacao,
            tipo_operacao=self.tipo.value,
            valor=self.valor,
            id_conta=self.id_conta,
            havia_efeito=self.aplicacao_confirmada_em is not None,
        )
        return replace(
            self,
            status=StatusAutorizacao.COMPENSADA,
            atualizada_em=agora,
            eventos=(*self.eventos, evento),
        )

    def encaminhar_para_reconciliacao(
        self, motivo: MotivoDaReconciliacao, agora: datetime, id_evento: str
    ) -> Autorizacao:
        """Sai do caminho automático e vai para a fila humana (índice próprio + alarme + evento), com o
        motivo e o progresso da compensação gravados para quem for decidir."""
        self._exigir_um_de(EM_CURSO, "encaminhar para reconciliação")
        return self._para_a_fila_humana(motivo, agora, id_evento)

    def encaminhar_reserva_sem_hold(self, agora: datetime, id_evento: str) -> Autorizacao:
        """RESERVA vencida, ainda APROVADA, cujo hold sumiu e cujo vencedor não apareceu: o status passa a
        dizer a verdade (não é mais uma reserva viva) e o caso vai para a fila humana."""
        if not self.reserva_pendente:
            raise TransicaoInvalida(
                "só reserva aprovada e pendente vai para a fila por falta de hold"
            )
        return self._para_a_fila_humana(MotivoDaReconciliacao.RESERVA_SEM_HOLD, agora, id_evento)

    def _para_a_fila_humana(
        self, motivo: MotivoDaReconciliacao, agora: datetime, id_evento: str
    ) -> Autorizacao:
        evento = TransacaoEmReconciliacaoManual(
            **self._base_do_evento(id_evento, agora),
            id_autorizacao=self.id_autorizacao,
            tipo_operacao=self.tipo.value,
            valor=self.valor,
            id_conta=self.id_conta,
            motivo=motivo.value,
            aplicacao_confirmada_em=self.aplicacao_confirmada_em,
            desfazer_desde=self.desfazer_desde,
        )
        return replace(
            self,
            status=StatusAutorizacao.RECONCILIACAO_MANUAL,
            motivo_da_reconciliacao=motivo,
            atualizada_em=agora,
            eventos=(*self.eventos, evento),
        )

    def estornar(self, agora: datetime) -> Autorizacao:
        """Marca a ORIGINAL como estornada (o evento sai da autorização de ESTORNO)."""
        self._exigir(StatusAutorizacao.APROVADA, "estornar")
        if self.tipo not in (TipoOperacao.RESERVA, TipoOperacao.DEBITO):
            raise TransicaoInvalida(f"{self.tipo} não é estornável")
        return replace(self, status=StatusAutorizacao.ESTORNADA, atualizada_em=agora)

    def capturar(self, agora: datetime) -> Autorizacao:
        """Marca a RESERVA original como capturada (os eventos saem da autorização de captura)."""
        self._exigir(StatusAutorizacao.APROVADA, "capturar")
        if self.tipo is not TipoOperacao.RESERVA:
            raise TransicaoInvalida(f"{self.tipo} não é capturável")
        return replace(self, status=StatusAutorizacao.CAPTURADA, atualizada_em=agora)

    def expirar(self, saldos: SaldosDoLimite, agora: datetime, id_evento: str) -> Autorizacao:
        self._exigir(StatusAutorizacao.APROVADA, "expirar")
        expira_em = self.expira_em
        if expira_em is None or expira_em > agora:
            raise TransicaoInvalida("só reserva aprovada e vencida expira")
        evento = ReservaExpirada(
            **self._base_do_evento(id_evento, agora),
            id_reserva=self.id_autorizacao,
            valor=self.valor,
            saldos=saldos,
        )
        return replace(
            self,
            status=StatusAutorizacao.EXPIRADA,
            atualizada_em=agora,
            eventos=(*self.eventos, evento),
        )

    def sem_eventos(self) -> Autorizacao:
        """Depois que o repositório gravou os eventos no outbox."""
        return replace(self, eventos=())

    def _exigir(self, esperado: StatusAutorizacao, acao: str) -> None:
        self._exigir_um_de((esperado,), acao)

    def _exigir_um_de(self, esperados: tuple[StatusAutorizacao, ...], acao: str) -> None:
        if self.status not in esperados:
            raise TransicaoInvalida(f"não é possível {acao} a partir de {self.status}")

    def _renovada(self, agora: datetime, **mudancas: object) -> Autorizacao:
        """Renova o lease a partir de `agora`. O reconciliador relê o relógio a cada passo, então cada passo
        (no máximo uma chamada ao limite) começa com um lease inteiro à frente."""
        return replace(
            self,
            lease_ate=agora + DURACAO_DO_LEASE,
            atualizada_em=agora,
            **mudancas,  # type: ignore[arg-type]
        )

    def _base_do_evento(self, id_evento: str, agora: datetime) -> _BaseDoEvento:
        return {
            "id_evento": id_evento,
            "ocorrido_em": agora,
            "id_contrato": self.id_contrato,
            "correlation_id": self.correlation_id,
            "traceparent": self.traceparent,
        }
