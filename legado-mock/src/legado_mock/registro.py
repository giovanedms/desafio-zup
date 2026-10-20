"""Formatador do registro de tamanho fixo do core legado (layout de 84 posições).

O legado NÃO conhece o BC Limites: este módulo reimplementa o layout do zero (copybook), sem importar
nada de `limites`. Quem prova que os dois lados concordam é o teste de contrato
`tests/contract/test_layout_com_acl.py`.

    pos  tam  campo              regra
    1    2    TIPO-REG           "LC" (limite de crédito)
    3    20   NUM-CONTRATO       alfanumérico, à esquerda, completado com espaços
    23   16   NUM-CONTA          idem
    39   2    COD-PRODUTO        01 crédito · 02 investimento · 03 seguro · 04 consórcio
    41   17   VLR-LIMITE         inteiro com 2 decimais IMPLÍCITOS (00000000000150000 = 1.500,00)
    58   1    COD-SITUACAO       A ativo · B bloqueado · E encerrado
    59   12   NUM-VERSAO         versão monotônica do registro no legado
    71   14   DT-HR-ATUALIZACAO  AAAAMMDDHHMMSS no horário de Brasília
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

TAMANHO_DO_REGISTRO = 84
FUSO_DO_LEGADO = ZoneInfo("America/Sao_Paulo")
PRODUTOS = frozenset({"01", "02", "03", "04"})
SITUACOES = frozenset({"A", "B", "E"})


def _agora() -> datetime:
    return datetime.now(FUSO_DO_LEGADO)


@dataclass(frozen=True, slots=True)
class RegistroLC:
    contrato: str
    conta: str
    produto: str
    limite: Decimal
    situacao: str
    versao: int
    atualizado_em: datetime = field(default_factory=_agora)

    def __post_init__(self) -> None:
        if not 0 < len(self.contrato) <= 20 or not 0 < len(self.conta) <= 16:
            raise ValueError("contrato (até 20) e conta (até 16) são obrigatórios")
        if self.produto not in PRODUTOS:
            raise ValueError(f"produto inválido: {self.produto!r}")
        if self.situacao not in SITUACOES:
            raise ValueError(f"situação inválida: {self.situacao!r}")
        if self.limite < 0 or self.limite != self.limite.quantize(Decimal("0.01")):
            raise ValueError("limite deve ser >= 0 com no máximo 2 casas decimais")
        if not 1 <= self.versao < 10**12:
            raise ValueError("versão deve caber em 12 posições e começar em 1")


def formatar(registro: RegistroLC) -> str:
    """RegistroLC → string de 84 posições."""
    centavos = int(registro.limite * 100)
    local = registro.atualizado_em.astimezone(FUSO_DO_LEGADO).strftime("%Y%m%d%H%M%S")
    texto = (
        f"LC{registro.contrato:<20}{registro.conta:<16}{registro.produto}"
        f"{centavos:017d}{registro.situacao}{registro.versao:012d}{local}"
    )
    if len(texto) != TAMANHO_DO_REGISTRO:  # pragma: no cover - guarda contra mudança de layout
        raise ValueError(f"registro com {len(texto)} posições")
    return texto
