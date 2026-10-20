"""Lançamento contábil em partidas dobradas — o agregado do BC Contabilização.

Invariante: Σ débitos = Σ créditos, com pelo menos uma partida de cada natureza e valores > 0.
O lançamento é imutável: correção se faz com OUTRO lançamento (estorno), nunca editando o anterior —
é isso que dá trilha de auditoria (Circular BCB 3.978: registro de todas as operações).

Plano de contas ILUSTRATIVO, inspirado nos grupos do COSIF (1 ativo circulante, 3 compensação ativa,
4 passivo circulante, 9 compensação passiva). Em produção o mapeamento evento → conta COSIF é
parametrizado pela área contábil, não codificado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum

NAMESPACE_LANCAMENTOS = uuid.UUID("c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f")


class Conta(Enum):
    OPERACOES_DE_CREDITO = ("1.6.1", "Operações de crédito — carteira de clientes")
    DISPONIBILIDADES = ("1.1.1", "Disponibilidades — recebimentos de clientes")
    COMPENSACAO_LIMITES_COMPROMETIDOS = ("3.0.1", "Compensação ativa — limites comprometidos")
    OBRIGACOES_COM_ESTABELECIMENTOS = ("4.9.9", "Transações a liquidar com estabelecimentos")
    COMPENSACAO_RESPONSABILIDADE_POR_LIMITES = (
        "9.0.1",
        "Compensação passiva — responsabilidade por limites",
    )

    @property
    def codigo(self) -> str:
        return self.value[0]

    @property
    def nome(self) -> str:
        return self.value[1]


class Natureza(StrEnum):
    DEBITO = "D"
    CREDITO = "C"


class LancamentoDesbalanceado(ValueError):
    """Partidas que não fecham: bug na regra de contabilização, nunca dado de entrada."""


@dataclass(frozen=True, slots=True)
class Partida:
    conta: Conta
    natureza: Natureza
    valor: Decimal


@dataclass(frozen=True, slots=True)
class Lancamento:
    id_lancamento: str
    id_evento_origem: str
    tipo_evento_origem: str
    id_contrato: str
    data_contabil: date
    ocorrido_em: datetime
    historico: str
    partidas: tuple[Partida, ...]

    def __post_init__(self) -> None:
        if any(p.valor <= 0 for p in self.partidas):
            raise LancamentoDesbalanceado("partida com valor não positivo")
        debitos = sum((p.valor for p in self.partidas if p.natureza is Natureza.DEBITO), Decimal(0))
        creditos = sum(
            (p.valor for p in self.partidas if p.natureza is Natureza.CREDITO), Decimal(0)
        )
        if debitos == 0 or creditos == 0:
            raise LancamentoDesbalanceado("lançamento precisa de débito E crédito")
        if debitos != creditos:
            raise LancamentoDesbalanceado(f"débitos {debitos} ≠ créditos {creditos}")

    @staticmethod
    def id_para(source: str, id_evento: str) -> str:
        """Determinístico: o mesmo evento (mesmo source+id) gera sempre o mesmo lançamento."""
        return str(uuid.uuid5(NAMESPACE_LANCAMENTOS, f"{source}|{id_evento}"))

    def saldo_por_conta(self) -> dict[Conta, Decimal]:
        """Efeito do lançamento em cada conta (débito positivo, crédito negativo)."""
        efeito: dict[Conta, Decimal] = {}
        for p in self.partidas:
            sinal = 1 if p.natureza is Natureza.DEBITO else -1
            efeito[p.conta] = efeito.get(p.conta, Decimal(0)) + sinal * p.valor
        return efeito


def partidas_dobradas(debito: Conta, credito: Conta, valor: Decimal) -> tuple[Partida, ...]:
    return (Partida(debito, Natureza.DEBITO, valor), Partida(credito, Natureza.CREDITO, valor))
