"""Pedido de autorização (comando) e suas regras de forma — independentes de HTTP.

Dinheiro aqui é Decimal com 2 casas. Este BC tem sua própria cópia do conceito (autonomia do bounded
context); se mais contextos precisarem, vira Shared Kernel numa lib (ADR-0015).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType

MOEDA_ACEITA = "BRL"
LIMITE_ABSOLUTO = Decimal("9999999999999999.99")


class TipoOperacao(StrEnum):
    DEBITO = "DEBITO"
    CREDITO = "CREDITO"
    RESERVA = "RESERVA"
    ESTORNO = "ESTORNO"


_SEM_ORIGINAL = frozenset({TipoOperacao.RESERVA, TipoOperacao.CREDITO})


class PedidoInvalido(ValueError):
    """Regra de forma do pedido violada (vira 422 VALIDACAO_DE_DOMINIO). `campo` aponta a origem."""

    def __init__(self, campo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.campo = campo
        self.mensagem = mensagem


def valor_monetario(valor: Decimal) -> Decimal:
    """Valor > 0, até 2 casas, em decimal(18,2). Float nunca chega (parse exato no adapter)."""
    if isinstance(valor, float) or not isinstance(valor, Decimal):
        raise PedidoInvalido("valor", "valor precisa ser decimal exato")
    try:
        expoente = valor.as_tuple().exponent
    except InvalidOperation as erro:  # pragma: no cover - Decimal válido sempre tem tupla
        raise PedidoInvalido("valor", "valor inválido") from erro
    if not valor.is_finite() or (isinstance(expoente, int) and expoente < -2):
        raise PedidoInvalido("valor", "valor com mais de 2 casas decimais")
    if valor <= 0:
        raise PedidoInvalido("valor", "valor precisa ser maior que zero")
    if valor > LIMITE_ABSOLUTO:
        raise PedidoInvalido("valor", "valor fora de decimal(18,2)")
    return valor.quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class PedidoDeAutorizacao:
    id_contrato: str
    id_conta: str
    valor: Decimal
    moeda: str
    tipo: TipoOperacao
    id_estabelecimento: str | None = None
    id_autorizacao_original: str | None = None
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "valor", valor_monetario(self.valor))
        if self.moeda != MOEDA_ACEITA:
            raise PedidoInvalido("moeda", "na v1 só BRL é aceito")
        if self.tipo is TipoOperacao.ESTORNO and not self.id_autorizacao_original:
            raise PedidoInvalido(
                "id_autorizacao_original", "obrigatório quando tipo_operacao=ESTORNO"
            )
        if self.tipo in _SEM_ORIGINAL and self.id_autorizacao_original:
            raise PedidoInvalido(
                "id_autorizacao_original", "só faz sentido em ESTORNO ou DEBITO (captura)"
            )

    @property
    def eh_captura(self) -> bool:
        """DEBITO que aponta para uma RESERVA aprovada: liquida o hold em vez de consumir limite novo."""
        return self.tipo is TipoOperacao.DEBITO and self.id_autorizacao_original is not None

    def forma_canonica(self) -> dict[str, object]:
        """Conteúdo semântico normalizado para a impressão digital (150 e 150.00: mesmo pedido)."""
        return {
            "id_contrato": self.id_contrato,
            "id_conta": self.id_conta,
            "valor": f"{self.valor:.2f}",
            "moeda": self.moeda,
            "tipo_operacao": self.tipo.value,
            "id_estabelecimento": self.id_estabelecimento,
            "id_autorizacao_original": self.id_autorizacao_original,
            "metadata": _normalizar(self.metadata),
        }


def _normalizar(valor: object) -> object:
    if isinstance(valor, Mapping):
        return {str(k): _normalizar(v) for k, v in sorted(valor.items())}
    if isinstance(valor, list | tuple):
        return [_normalizar(v) for v in valor]
    if isinstance(valor, Decimal):
        return str(valor.normalize())
    return valor
