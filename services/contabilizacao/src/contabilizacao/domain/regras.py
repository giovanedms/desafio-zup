"""Regras de contabilização: evento de domínio (CloudEvent) → lançamento em partidas dobradas.

Uma regra por tipo de evento (Strategy): evento novo = regra nova registrada, sem tocar nas outras (OCP).
Leitor tolerante: só os campos necessários são lidos; campos novos no evento não quebram nada.
Reordenação: cada evento gera um lançamento AUTOCONTIDO e saldo de conta é soma (comutativa) — um estorno
que chega antes da autorização original apenas lança a reversão; o saldo final é o mesmo em qualquer ordem.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from contabilizacao.domain.lancamento import Conta, Lancamento, partidas_dobradas

FUSO_CONTABIL = ZoneInfo("America/Sao_Paulo")  # data contábil é a data local do fato, não a UTC


class EventoInvalido(ValueError):
    """CloudEvent sem os campos mínimos para contabilizar (vai para a DLQ após as tentativas)."""


@dataclass(frozen=True, slots=True)
class EventoRecebido:
    """O mínimo de um CloudEvent que este BC precisa (leitor tolerante)."""

    id: str
    source: str
    nome: str  # nome curto, ex.: TransacaoAutorizada (vem de data.event_type)
    ocorrido_em: datetime
    data: Mapping[str, Any]

    @classmethod
    def de_cloudevent(cls, cloudevent: Mapping[str, Any]) -> EventoRecebido:
        try:
            data = cloudevent["data"]
            return cls(
                id=str(cloudevent["id"]),
                source=str(cloudevent["source"]),
                nome=str(data["event_type"]),
                ocorrido_em=datetime.fromisoformat(str(cloudevent["time"]).replace("Z", "+00:00")),
                data=data,
            )
        except (KeyError, TypeError, ValueError) as erro:
            raise EventoInvalido(f"CloudEvent incompleto: {erro}") from erro

    def valor(self) -> Decimal:
        try:
            return Decimal(str(self.data["valor"]))
        except (KeyError, ArithmeticError) as erro:
            raise EventoInvalido("evento sem valor válido") from erro


class RegraDeContabilizacao(ABC):
    nome_do_evento: str

    @abstractmethod
    def lancar(self, evento: EventoRecebido) -> Lancamento: ...

    def _lancamento(
        self,
        evento: EventoRecebido,
        historico: str,
        debito: Conta,
        credito: Conta,
        valor: Decimal | None = None,
    ) -> Lancamento:
        return Lancamento(
            id_lancamento=Lancamento.id_para(evento.source, evento.id),
            id_evento_origem=evento.id,
            tipo_evento_origem=evento.nome,
            id_contrato=str(evento.data.get("id_contrato", "")),
            data_contabil=evento.ocorrido_em.astimezone(FUSO_CONTABIL).date(),
            ocorrido_em=evento.ocorrido_em,
            historico=historico,
            partidas=partidas_dobradas(debito, credito, evento.valor() if valor is None else valor),
        )


class ContabilizarTransacaoAutorizada(RegraDeContabilizacao):
    nome_do_evento = "TransacaoAutorizada"

    def lancar(self, evento: EventoRecebido) -> Lancamento:
        tipo = evento.data.get("tipo_operacao")
        aut = evento.data.get("id_autorizacao")
        if tipo == "DEBITO":
            return self._lancamento(
                evento,
                f"Débito autorizado {aut}",
                Conta.OPERACOES_DE_CREDITO,
                Conta.OBRIGACOES_COM_ESTABELECIMENTOS,
            )
        if tipo == "CREDITO":
            return self._lancamento(
                evento,
                f"Pagamento recebido {aut}",
                Conta.DISPONIBILIDADES,
                Conta.OPERACOES_DE_CREDITO,
            )
        if tipo == "RESERVA":
            return self._lancamento(
                evento,
                f"Reserva de limite {aut}",
                Conta.COMPENSACAO_LIMITES_COMPROMETIDOS,
                Conta.COMPENSACAO_RESPONSABILIDADE_POR_LIMITES,
            )
        raise EventoInvalido(f"tipo_operacao desconhecido: {tipo!r}")


class ContabilizarTransacaoEstornada(RegraDeContabilizacao):
    """Estorno é o lançamento INVERSO do original — o original nunca é editado."""

    nome_do_evento = "TransacaoEstornada"

    def lancar(self, evento: EventoRecebido) -> Lancamento:
        original = evento.data.get("tipo_operacao_original")
        ref = evento.data.get("id_autorizacao_original")
        if original == "DEBITO":
            return self._lancamento(
                evento,
                f"Estorno do débito {ref}",
                Conta.OBRIGACOES_COM_ESTABELECIMENTOS,
                Conta.OPERACOES_DE_CREDITO,
            )
        if original == "RESERVA":
            return self._lancamento(
                evento,
                f"Estorno da reserva {ref}",
                Conta.COMPENSACAO_RESPONSABILIDADE_POR_LIMITES,
                Conta.COMPENSACAO_LIMITES_COMPROMETIDOS,
            )
        raise EventoInvalido(f"tipo_operacao_original desconhecido: {original!r}")


class ContabilizarReservaCapturada(RegraDeContabilizacao):
    """Baixa da compensação pelo valor RESERVADO inteiro (sobra de captura parcial volta ao limite).

    O débito capturado é contabilizado pela TransacaoAutorizada irmã (DEBITO): um evento, um lançamento.
    """

    nome_do_evento = "ReservaCapturada"

    def lancar(self, evento: EventoRecebido) -> Lancamento:
        try:
            reservado = Decimal(str(evento.data["valor_reservado"]))
        except (KeyError, ArithmeticError) as erro:
            raise EventoInvalido("ReservaCapturada sem valor_reservado válido") from erro
        return self._lancamento(
            evento,
            f"Captura da reserva {evento.data.get('id_reserva')}",
            Conta.COMPENSACAO_RESPONSABILIDADE_POR_LIMITES,
            Conta.COMPENSACAO_LIMITES_COMPROMETIDOS,
            reservado,
        )


class ContabilizarReservaExpirada(RegraDeContabilizacao):
    nome_do_evento = "ReservaExpirada"

    def lancar(self, evento: EventoRecebido) -> Lancamento:
        return self._lancamento(
            evento,
            f"Expiração da reserva {evento.data.get('id_reserva')}",
            Conta.COMPENSACAO_RESPONSABILIDADE_POR_LIMITES,
            Conta.COMPENSACAO_LIMITES_COMPROMETIDOS,
        )


REGRAS_PADRAO: tuple[RegraDeContabilizacao, ...] = (
    ContabilizarTransacaoAutorizada(),
    ContabilizarTransacaoEstornada(),
    ContabilizarReservaExpirada(),
    ContabilizarReservaCapturada(),
)


class LivroDeRegras:
    """Registro de regras por nome de evento. Evento sem regra é ignorado (não é contábil)."""

    def __init__(self, regras: tuple[RegraDeContabilizacao, ...] = REGRAS_PADRAO) -> None:
        self._regras = {r.nome_do_evento: r for r in regras}

    def lancar(self, evento: EventoRecebido) -> Lancamento | None:
        regra = self._regras.get(evento.nome)
        return regra.lancar(evento) if regra else None
