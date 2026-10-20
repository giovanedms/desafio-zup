"""Resultado de uma operação no limite — recusa é resultado de negócio, não exceção."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Desfecho(StrEnum):
    APLICADA = "APLICADA"
    JA_APLICADA = "JA_APLICADA"
    RECUSADA = "RECUSADA"


class MotivoRecusa(StrEnum):
    LIMITE_INSUFICIENTE = "LIMITE_INSUFICIENTE"
    CONTRATO_INATIVO = "CONTRATO_INATIVO"
    CREDITO_EXCEDE_UTILIZADO = "CREDITO_EXCEDE_UTILIZADO"
    RESERVA_DIVERGENTE = "RESERVA_DIVERGENTE"  # valor ≠ do hold gravado: bug no chamador
    RESERVA_NAO_ENCONTRADA = "RESERVA_NAO_ENCONTRADA"  # hold já capturado, liberado ou expirado


@dataclass(frozen=True, slots=True)
class Saldos:
    limite_concedido: Decimal
    limite_utilizado: Decimal
    saldo_reservado: Decimal
    limite_disponivel: Decimal
    versao: int


@dataclass(frozen=True, slots=True)
class ResultadoDaOperacao:
    desfecho: Desfecho
    saldos: Saldos
    motivo: MotivoRecusa | None = None

    def __post_init__(self) -> None:
        if (self.desfecho is Desfecho.RECUSADA) != (self.motivo is not None):
            raise ValueError("motivo é obrigatório só (e somente) quando o desfecho é RECUSADA")
