"""Disjuntor com relógio falso: transições FECHADO -> ABERTO -> MEIO_ABERTO -> FECHADO/ABERTO."""

from __future__ import annotations

from autorizacao.adapters.outbound.disjuntor import Disjuntor, EstadoDoDisjuntor


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 100.0

    def __call__(self) -> float:
        return self.agora


def _aberto(relogio: RelogioFalso) -> Disjuntor:
    disjuntor = Disjuntor(limiar_falhas=5, pausa_s=5.0, relogio=relogio)
    for _ in range(5):
        assert disjuntor.permitir()
        disjuntor.registrar_falha()
    return disjuntor


def test_abre_apos_cinco_falhas_consecutivas() -> None:
    relogio = RelogioFalso()
    disjuntor = Disjuntor(relogio=relogio)
    for _ in range(4):
        disjuntor.registrar_falha()
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO

    disjuntor.registrar_falha()

    assert disjuntor.estado is EstadoDoDisjuntor.ABERTO


def test_sucesso_zera_a_contagem_de_falhas() -> None:
    disjuntor = Disjuntor(relogio=RelogioFalso())
    for _ in range(4):
        disjuntor.registrar_falha()
    disjuntor.registrar_sucesso()
    for _ in range(4):
        disjuntor.registrar_falha()

    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


def test_aberto_rejeita_ate_a_pausa_vencer() -> None:
    relogio = RelogioFalso()
    disjuntor = _aberto(relogio)

    assert not disjuntor.permitir()
    relogio.agora += 4.9
    assert not disjuntor.permitir()
    relogio.agora += 0.1
    assert disjuntor.estado is EstadoDoDisjuntor.MEIO_ABERTO


def test_meio_aberto_deixa_passar_uma_unica_prova() -> None:
    relogio = RelogioFalso()
    disjuntor = _aberto(relogio)
    relogio.agora += 5.0

    assert disjuntor.permitir()  # a prova
    assert not disjuntor.permitir()  # concorrentes rejeitadas
    assert not disjuntor.permitir()


def test_sucesso_na_prova_fecha_e_zera() -> None:
    relogio = RelogioFalso()
    disjuntor = _aberto(relogio)
    relogio.agora += 5.0
    assert disjuntor.permitir()

    disjuntor.registrar_sucesso()

    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO
    assert disjuntor.permitir()
    for _ in range(4):  # contagem recomeçou do zero
        disjuntor.registrar_falha()
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


def test_falha_na_prova_reabre_com_pausa_completa() -> None:
    relogio = RelogioFalso()
    disjuntor = _aberto(relogio)
    relogio.agora += 5.0
    assert disjuntor.permitir()

    disjuntor.registrar_falha()

    assert disjuntor.estado is EstadoDoDisjuntor.ABERTO
    assert not disjuntor.permitir()
    relogio.agora += 4.9
    assert not disjuntor.permitir()
    relogio.agora += 0.1
    assert disjuntor.permitir()
