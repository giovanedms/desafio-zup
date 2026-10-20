"""Anticorruption Layer: registro de tamanho fixo do core legado → AtualizacaoDoLegado (domínio).

Layout do registro (84 posições, estilo copybook de mainframe, numérico com zeros à esquerda):

    pos  tam  campo              exemplo / regra
    1    2    TIPO-REG           "LC" (limite de crédito) — outros tipos são ignorados
    3    20   NUM-CONTRATO       alfanumérico, alinhado à esquerda, completado com espaços
    23   16   NUM-CONTA          idem
    39   2    COD-PRODUTO        01 crédito · 02 investimento · 03 seguro · 04 consórcio
    41   17   VLR-LIMITE         inteiro com 2 decimais IMPLÍCITOS (00000000000150000 = 1.500,00)
    58   1    COD-SITUACAO       A ativo · B bloqueado · E encerrado
    59   12   NUM-VERSAO         versão monotônica do registro no legado
    71   14   DT-HR-ATUALIZACAO  AAAAMMDDHHMMSS no horário de Brasília

Tudo que é "do legado" (posições, códigos, decimal implícito, fuso local) morre aqui: o domínio recebe
tipos e nomes da linguagem ubíqua. Registro inválido levanta `RegistroLegadoInvalido` (vai para a DLQ).

Os tamanhos do layout SÃO o contrato de identificadores: os patterns `IdContrato` ({1,20}) e `id_conta`
({1,16}) dos OpenAPI saem daqui. Nada é truncado: id que não cabe falha alto, nos dois sentidos.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from limites.domain.legado import AtualizacaoDoLegado
from limites.domain.limite import StatusContrato, TipoProduto

TAMANHO_DO_REGISTRO = 84
TAMANHO_DO_CONTRATO = 20
TAMANHO_DA_CONTA = 16
DIGITOS_DO_VALOR = 17
_ID_DO_CONTRATO = re.compile(rf"^[A-Za-z0-9-]{{1,{TAMANHO_DO_CONTRATO}}}$")
_ID_DA_CONTA = re.compile(rf"^[A-Za-z0-9-]{{1,{TAMANHO_DA_CONTA}}}$")
FUSO_DO_LEGADO = ZoneInfo("America/Sao_Paulo")
_PRODUTOS = {
    "01": TipoProduto.CREDITO,
    "02": TipoProduto.INVESTIMENTO,
    "03": TipoProduto.SEGURO,
    "04": TipoProduto.CONSORCIO,
}
_SITUACOES = {
    "A": StatusContrato.ATIVO,
    "B": StatusContrato.BLOQUEADO,
    "E": StatusContrato.ENCERRADO,
}


class RegistroLegadoInvalido(ValueError):
    """Registro fora do layout acordado com o legado."""


def _campo(registro: str, inicio: int, tamanho: int) -> str:
    return registro[inicio - 1 : inicio - 1 + tamanho]


def traduzir(registro: str) -> AtualizacaoDoLegado | None:
    """Registro LC → comando de domínio. Outros tipos de registro → None (não interessam a este BC)."""
    registro = registro.rstrip("\r\n")
    if len(registro) != TAMANHO_DO_REGISTRO:
        raise RegistroLegadoInvalido(
            f"registro com {len(registro)} posições (esperado {TAMANHO_DO_REGISTRO})"
        )
    if _campo(registro, 1, 2) != "LC":
        return None
    try:
        produto = _PRODUTOS[_campo(registro, 39, 2)]
        situacao = _SITUACOES[_campo(registro, 58, 1)]
        centavos = int(_campo(registro, 41, 17))
        versao = int(_campo(registro, 59, 12))
        atualizado_local = datetime.strptime(_campo(registro, 71, 14), "%Y%m%d%H%M%S")
    except (KeyError, ValueError) as erro:
        raise RegistroLegadoInvalido(f"campo inválido: {erro}") from erro
    id_contrato = _campo(registro, 3, TAMANHO_DO_CONTRATO).strip()
    id_conta = _campo(registro, 23, TAMANHO_DA_CONTA).strip()
    if not _ID_DO_CONTRATO.match(id_contrato) or not _ID_DA_CONTA.match(id_conta):
        # Vazio ou com caractere fora do pattern da API: contrato que nenhum canal conseguiria endereçar.
        raise RegistroLegadoInvalido("contrato ou conta ausente ou fora do pattern da API")
    try:
        return AtualizacaoDoLegado(
            id_contrato=id_contrato,
            id_conta=id_conta,
            tipo_produto=produto,
            concedido=Decimal(centavos) / 100,
            status=situacao,
            versao_legado=versao,
            atualizado_em=atualizado_local.replace(tzinfo=FUSO_DO_LEGADO),
        )
    except ValueError as erro:
        raise RegistroLegadoInvalido(str(erro)) from erro


def formatar(atualizacao: AtualizacaoDoLegado) -> str:
    """Inverso de `traduzir` — usado pelo legado-mock e pelos testes (prova de ida e volta).

    O que o layout não representa levanta `RegistroLegadoInvalido` em vez de truncar: truncar
    faria dois contratos distintos virarem o mesmo registro.
    """
    if not _ID_DO_CONTRATO.match(atualizacao.id_contrato):
        raise RegistroLegadoInvalido(f"id_contrato não cabe em {TAMANHO_DO_CONTRATO} posições")
    if not _ID_DA_CONTA.match(atualizacao.id_conta):
        raise RegistroLegadoInvalido(f"id_conta não cabe em {TAMANHO_DA_CONTA} posições")
    produto = {v: k for k, v in _PRODUTOS.items()}[atualizacao.tipo_produto]
    situacao = {v: k for k, v in _SITUACOES.items()}[atualizacao.status]
    centavos = int(atualizacao.concedido * 100)
    if centavos >= 10**DIGITOS_DO_VALOR:
        raise RegistroLegadoInvalido(f"limite não cabe em {DIGITOS_DO_VALOR} dígitos")
    local = atualizacao.atualizado_em.astimezone(FUSO_DO_LEGADO).strftime("%Y%m%d%H%M%S")
    return (
        f"LC{atualizacao.id_contrato:<20}{atualizacao.id_conta:<16}{produto}"
        f"{centavos:017d}{situacao}{atualizacao.versao_legado:012d}{local}"
    )
