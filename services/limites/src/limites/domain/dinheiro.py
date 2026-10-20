"""Dinheiro: value object para valores decimal(18,2) em BRL, sem ponto flutuante."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

CASAS_DECIMAIS = 2
LIMITE_ABSOLUTO = Decimal("9999999999999999.99")  # decimal(18,2)
ZERO = Decimal("0.00")


class ValorMonetarioInvalido(ValueError):
    """Valor que não cabe em decimal(18,2) ou que veio como float."""


def dinheiro(valor: Decimal | int | str) -> Decimal:
    """Converte para Decimal com exatamente 2 casas, recusando float e escala maior que 2.

    Float é recusado de propósito: 0.1 em binário não é 0.1. O adapter HTTP entrega Decimal
    lido direto do texto do JSON; aqui só garantimos a forma.
    """
    if isinstance(valor, float):
        raise ValorMonetarioInvalido("valor monetário não pode ser float")
    try:
        decimal = Decimal(valor)
    except (InvalidOperation, ValueError) as erro:
        raise ValorMonetarioInvalido(f"valor monetário inválido: {valor!r}") from erro
    if not decimal.is_finite():
        raise ValorMonetarioInvalido("valor monetário precisa ser finito")
    expoente = decimal.as_tuple().exponent
    if isinstance(expoente, int) and expoente < -CASAS_DECIMAIS:
        raise ValorMonetarioInvalido("valor monetário com mais de 2 casas decimais")
    if abs(decimal) > LIMITE_ABSOLUTO:
        raise ValorMonetarioInvalido("valor monetário fora de decimal(18,2)")
    return decimal.quantize(Decimal("0.01"))


def dinheiro_positivo(valor: Decimal | int | str) -> Decimal:
    """Como `dinheiro`, exigindo valor > 0 (regra do enunciado para `valor`)."""
    convertido = dinheiro(valor)
    if convertido <= ZERO:
        raise ValorMonetarioInvalido("valor da operação precisa ser maior que zero")
    return convertido
