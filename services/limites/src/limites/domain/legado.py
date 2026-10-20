"""Sincronização com o core banking legado — regras do lado do domínio (a tradução de formato é da ACL).

Autoridade por atributo durante a migração (ADR-0014):
  - legado é dono de: existência do contrato, conta, produto, LIMITE CONCEDIDO e STATUS;
  - este BC é dono de: utilizado, reservado e reservas (saldo em tempo real).
A atualização do legado nunca toca utilizado/reservado; o disponível acompanha a variação do concedido.

O feed do legado pode chegar fora de ordem e repetido: cada registro traz uma versão monotônica; versão
menor ou igual à já aplicada é OBSOLETA e ignorada (idempotência e ordem resolvidas pela versão).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from limites.domain.dinheiro import dinheiro
from limites.domain.limite import Limite, StatusContrato, TipoProduto


@dataclass(frozen=True, slots=True)
class AtualizacaoDoLegado:
    """Comando de domínio já traduzido pela ACL (nenhum conceito do formato legado vaza para cá)."""

    id_contrato: str
    id_conta: str
    tipo_produto: TipoProduto
    concedido: Decimal
    status: StatusContrato
    versao_legado: int
    atualizado_em: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "concedido", dinheiro(self.concedido))
        if self.concedido < 0:
            raise ValueError("limite concedido não pode ser negativo")
        if self.versao_legado < 1:
            raise ValueError("versão do legado começa em 1")


class ResultadoDaSincronizacao(StrEnum):
    CRIADO = "CRIADO"  # carga inicial / contrato novo
    ATUALIZADO = "ATUALIZADO"  # concedido e/ou status mudaram → LimiteAlterado (via CDC)
    SEM_MUDANCA = "SEM_MUDANCA"  # versão nova, mesmos valores: só registra a versão
    OBSOLETA = "OBSOLETA"  # versão ≤ aplicada: fora de ordem ou repetida, ignorada


def sincronizar(
    atual: Limite | None, atualizacao: AtualizacaoDoLegado
) -> tuple[Limite | None, ResultadoDaSincronizacao]:
    """Novo estado do limite (ou None se não há nada a gravar) e o que aconteceu."""
    if atual is None:
        novo = Limite.novo(
            atualizacao.id_contrato,
            atualizacao.id_conta,
            atualizacao.tipo_produto,
            atualizacao.concedido,
            status=atualizacao.status,
            versao_legado=atualizacao.versao_legado,
        )
        return novo, ResultadoDaSincronizacao.CRIADO
    if atualizacao.versao_legado <= atual.versao_legado:
        return None, ResultadoDaSincronizacao.OBSOLETA
    mudou = atualizacao.concedido != atual.concedido or atualizacao.status is not atual.status
    if not mudou:
        return replace(atual, versao_legado=atualizacao.versao_legado), (
            ResultadoDaSincronizacao.SEM_MUDANCA
        )
    variacao = atualizacao.concedido - atual.concedido
    novo = replace(
        atual,
        concedido=atualizacao.concedido,
        disponivel=atual.disponivel + variacao,
        status=atualizacao.status,
        versao_legado=atualizacao.versao_legado,
        versao=atual.versao + 1,
    )
    novo.verificar_invariantes()
    return novo, ResultadoDaSincronizacao.ATUALIZADO
