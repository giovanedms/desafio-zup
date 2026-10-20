"""Eventos de domínio do BC Autorização: fatos no passado, imutáveis.

São objetos puros; o envelope CloudEvents (id, source, type, dataschema, traceparent...) é montado
pelo adapter do outbox. O `id_evento` vem de fora (gerador injetado): testes determinísticos.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SaldosDoLimite:
    """Retrato do limite logo após a operação, como devolvido pelo BC Limites."""

    limite_concedido: Decimal
    limite_utilizado: Decimal
    saldo_reservado: Decimal
    limite_disponivel: Decimal
    versao: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EventoDeDominio:
    id_evento: str
    ocorrido_em: datetime
    id_contrato: str
    correlation_id: str
    traceparent: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TransacaoAutorizada(EventoDeDominio):
    id_autorizacao: str
    tipo_operacao: str
    valor: Decimal
    moeda: str
    id_conta: str
    id_estabelecimento: str | None
    saldos: SaldosDoLimite
    id_autorizacao_original: str | None = None  # preenchido na captura (DEBITO de uma RESERVA)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransacaoRecusada(EventoDeDominio):
    id_autorizacao: str
    tipo_operacao: str
    motivo: str
    valor: Decimal
    moeda: str
    id_conta: str
    id_estabelecimento: str | None
    saldos: SaldosDoLimite | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TransacaoEstornada(EventoDeDominio):
    id_autorizacao: str
    id_autorizacao_original: str
    tipo_operacao_original: str
    valor: Decimal
    saldos: SaldosDoLimite


@dataclass(frozen=True, slots=True, kw_only=True)
class TransacaoCompensada(EventoDeDominio):
    """A autorização ficou sem resposta ao canal e a reconciliação a desfez: efeito líquido zero no limite.
    `havia_efeito`: o limite confirmou que a operação estava aplicada (e ela foi revertida)."""

    id_autorizacao: str
    tipo_operacao: str
    valor: Decimal
    id_conta: str
    havia_efeito: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class TransacaoEmReconciliacaoManual(EventoDeDominio):
    """Não deu para concluir com segurança de forma automática: decisão humana (fila RM#). O evento é a
    trilha durável do caso (o item da fila não tem TTL enquanto estiver lá, mas o evento fica 10 anos)."""

    id_autorizacao: str
    tipo_operacao: str
    valor: Decimal
    id_conta: str
    motivo: str
    aplicacao_confirmada_em: datetime | None
    desfazer_desde: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservaCapturada(EventoDeDominio):
    """O hold saiu do limite porque virou consumo (o débito em si é a TransacaoAutorizada irmã)."""

    id_reserva: str
    id_autorizacao: str  # a autorização de captura (DEBITO)
    valor_reservado: Decimal
    valor_capturado: Decimal
    saldos: SaldosDoLimite


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservaExpirada(EventoDeDominio):
    id_reserva: str
    valor: Decimal
    saldos: SaldosDoLimite
