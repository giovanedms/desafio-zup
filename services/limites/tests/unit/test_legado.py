"""ACL do core legado: tradução do registro de tamanho fixo e regras de sincronização no domínio."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from limites.adapters.inbound.legado.traducao import (
    RegistroLegadoInvalido,
    formatar,
    traduzir,
)
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao, sincronizar
from limites.domain.limite import Limite, StatusContrato, TipoProduto
from limites.domain.operacoes import Reservar
from limites.domain.resultados import Desfecho, MotivoRecusa

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
REGISTRO = (
    "LC"
    "CTR-000123456       "  # contrato (20)
    "0001-12345678-9 "  # conta (16)
    "01"  # produto crédito
    "00000000000150000"  # 1.500,00 com 2 decimais implícitos
    "A"  # ativo
    "000000000007"  # versão 7
    "20261020101500"  # 10:15:00 em Brasília
)


def atualizacao(
    concedido: str = "1500.00", versao: int = 7, status: StatusContrato = StatusContrato.ATIVO
) -> AtualizacaoDoLegado:
    return AtualizacaoDoLegado(
        "CTR-000123456",
        "0001-12345678-9",
        TipoProduto.CREDITO,
        Decimal(concedido),
        status,
        versao,
        AGORA,
    )


# --- ACL: tradução -----------------------------------------------------------------------------


def test_registro_legado_vira_comando_de_dominio() -> None:
    traduzida = traduzir(REGISTRO)

    assert traduzida is not None
    assert traduzida.id_contrato == "CTR-000123456"
    assert traduzida.concedido == Decimal("1500.00")
    assert traduzida.status is StatusContrato.ATIVO
    assert traduzida.versao_legado == 7
    assert traduzida.atualizado_em.utcoffset() is not None  # fuso de Brasília explícito


def test_ida_e_volta_do_formato_legado() -> None:
    traduzida = traduzir(REGISTRO)
    assert traduzida is not None
    assert formatar(traduzida) == REGISTRO


@pytest.mark.parametrize(
    ("id_contrato", "id_conta", "concedido"),
    [
        ("CTR-2026-SP-000000001-A", "0001-1", "1.00"),  # 23 posições: truncar faria colidir com -B
        ("CTR-1", "0001-12345678-9-XY", "1.00"),  # conta com 18 posições
        ("CTR-1", "0001-1", "1000000000000000.00"),  # 10^15: não cabe nos 17 dígitos
    ],
    ids=["contrato", "conta", "valor"],
)
def test_formatar_falha_em_vez_de_truncar(id_contrato: str, id_conta: str, concedido: str) -> None:
    grande = AtualizacaoDoLegado(
        id_contrato,
        id_conta,
        TipoProduto.CREDITO,
        Decimal(concedido),
        StatusContrato.ATIVO,
        1,
        AGORA,
    )

    with pytest.raises(RegistroLegadoInvalido):
        formatar(grande)


def test_contrato_com_caractere_fora_do_pattern_da_api_e_invalido() -> None:
    with pytest.raises(RegistroLegadoInvalido):
        traduzir(REGISTRO[:2] + "CTR/000123456       " + REGISTRO[22:])


def test_maior_id_do_layout_ida_e_volta() -> None:
    maior = AtualizacaoDoLegado(
        "C" * 20, "9" * 16, TipoProduto.CREDITO, Decimal("999999999999999.99"),
        StatusContrato.ATIVO, 1, AGORA,
    )  # fmt: skip

    traduzida = traduzir(formatar(maior))

    assert traduzida is not None
    assert (traduzida.id_contrato, traduzida.id_conta) == ("C" * 20, "9" * 16)
    assert traduzida.concedido == Decimal("999999999999999.99")


def test_outro_tipo_de_registro_e_ignorado() -> None:
    assert traduzir("XX" + REGISTRO[2:]) is None


@pytest.mark.parametrize(
    "registro",
    [
        REGISTRO[:-1],
        REGISTRO[:38] + "99" + REGISTRO[40:],
        REGISTRO[:57] + "Z" + REGISTRO[58:],
        REGISTRO[:40] + "0000000000000150X" + REGISTRO[57:],
    ],
    ids=["tamanho", "produto", "situacao", "valor"],
)
def test_registro_fora_do_layout_e_invalido(registro: str) -> None:
    with pytest.raises(RegistroLegadoInvalido):
        traduzir(registro)


# --- Domínio: sincronização --------------------------------------------------------------------


def test_contrato_novo_e_criado_pela_carga_do_legado() -> None:
    novo, resultado = sincronizar(None, atualizacao())

    assert resultado is ResultadoDaSincronizacao.CRIADO
    assert novo is not None and novo.disponivel == Decimal("1500.00")


def test_aumento_do_concedido_aumenta_o_disponivel_sem_tocar_reservas() -> None:
    atual, _ = sincronizar(None, atualizacao("1000.00", versao=1))
    assert atual is not None
    atual = atual.aplicar(Reservar("r1", Decimal("300.00")).planejar(), AGORA)

    novo, resultado = sincronizar(atual, atualizacao("1500.00", versao=2))

    assert resultado is ResultadoDaSincronizacao.ATUALIZADO
    assert novo is not None
    assert (novo.disponivel, novo.reservado, novo.versao) == (
        Decimal("1200.00"),
        Decimal("300.00"),
        atual.versao + 1,
    )


def test_reducao_abaixo_do_comprometido_deixa_disponivel_negativo_e_bloqueia_reservas() -> None:
    atual, _ = sincronizar(None, atualizacao("1000.00", versao=1))
    assert atual is not None
    atual = atual.aplicar(Reservar("r1", Decimal("800.00")).planejar(), AGORA)

    novo, _ = sincronizar(atual, atualizacao("500.00", versao=2))

    assert novo is not None and novo.disponivel == Decimal(
        "-300.00"
    )  # decisão do crédito prevalece
    assert novo.diagnosticar(Reservar("r2", Decimal("0.01"))) == (
        Desfecho.RECUSADA,
        MotivoRecusa.LIMITE_INSUFICIENTE,
    )


def test_versao_antiga_ou_repetida_e_obsoleta() -> None:
    atual, _ = sincronizar(None, atualizacao(versao=7))
    assert atual is not None

    for versao in (6, 7):
        novo, resultado = sincronizar(atual, atualizacao("9999.00", versao=versao))
        assert (novo, resultado) == (None, ResultadoDaSincronizacao.OBSOLETA)


def test_versao_nova_com_os_mesmos_valores_so_registra_a_versao() -> None:
    atual, _ = sincronizar(None, atualizacao(versao=7))
    assert atual is not None

    novo, resultado = sincronizar(atual, atualizacao(versao=8))

    assert resultado is ResultadoDaSincronizacao.SEM_MUDANCA
    assert novo is not None and (novo.versao_legado, novo.versao) == (8, atual.versao)


def test_bloqueio_pelo_legado_recusa_novas_reservas() -> None:
    atual, _ = sincronizar(None, atualizacao(versao=1))
    assert atual is not None

    novo, _ = sincronizar(atual, atualizacao(versao=2, status=StatusContrato.BLOQUEADO))

    assert novo is not None
    assert novo.diagnosticar(Reservar("r1", Decimal("1.00"))) == (
        Desfecho.RECUSADA,
        MotivoRecusa.CONTRATO_INATIVO,
    )


@settings(max_examples=150)
@given(
    versoes=st.lists(
        st.tuples(st.integers(1, 30), st.integers(0, 500_000)),
        min_size=1,
        max_size=15,
        unique_by=lambda v: v[0],
    ),
    embaralhar=st.randoms(use_true_random=False),
)
def test_feed_fora_de_ordem_converge_para_a_maior_versao(
    versoes: list[tuple[int, int]], embaralhar: object
) -> None:
    repositorio = RepositorioDeLimitesEmMemoria()
    chegada = list(versoes)
    embaralhar.shuffle(chegada)  # type: ignore[attr-defined]

    for versao, centavos in chegada:
        repositorio.sincronizar(atualizacao(str(Decimal(centavos) / 100), versao=versao))

    versao_maxima, centavos_da_maxima = max(versoes)
    final = repositorio.obter("CTR-000123456")
    assert final is not None
    assert (final.versao_legado, final.concedido) == (
        versao_maxima,
        Decimal(centavos_da_maxima) / 100,
    )
    final.verificar_invariantes()


def _limite_ativo() -> Limite:
    return Limite.novo("CTR-1", "0001-1", TipoProduto.CREDITO, Decimal("1000.00"))
