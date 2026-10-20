"""Partidas dobradas, regras por evento, idempotência (inbox) e tolerância a reordenação."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from contabilizacao.adapters.outbound.memoria import RepositorioContabilEmMemoria
from contabilizacao.application.contabilizar import ContabilizarEvento, Desfecho
from contabilizacao.domain.lancamento import (
    Conta,
    Lancamento,
    LancamentoDesbalanceado,
    Natureza,
    Partida,
)
from contabilizacao.domain.regras import EventoInvalido, EventoRecebido, LivroDeRegras
from hypothesis import given, settings
from hypothesis import strategies as st


def cloudevent(nome: str, valor: str = "150.00", **data: Any) -> dict[str, Any]:
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "/financas/autorizacao",
        "type": f"example.financas.autorizacao.{nome}.v1",
        "time": "2026-10-21T02:30:00.000Z",  # 23h30 do dia 20 em São Paulo
        "data": {"event_type": nome, "id_contrato": "CTR-1", "valor": Decimal(valor), **data},
    }


@pytest.fixture
def repositorio() -> RepositorioContabilEmMemoria:
    return RepositorioContabilEmMemoria()


@pytest.fixture
def caso_de_uso(repositorio: RepositorioContabilEmMemoria) -> ContabilizarEvento:
    return ContabilizarEvento(repositorio, LivroDeRegras())


def test_lancamento_desbalanceado_e_rejeitado() -> None:
    with pytest.raises(LancamentoDesbalanceado):
        Lancamento(
            "l1",
            "e1",
            "X",
            "CTR-1",
            date(2026, 10, 20),
            datetime.now(UTC),
            "h",
            (
                Partida(Conta.DISPONIBILIDADES, Natureza.DEBITO, Decimal(10)),
                Partida(Conta.OPERACOES_DE_CREDITO, Natureza.CREDITO, Decimal(9)),
            ),
        )


def test_debito_autorizado_gera_partidas_dobradas(
    caso_de_uso: ContabilizarEvento, repositorio: RepositorioContabilEmMemoria
) -> None:
    desfecho = caso_de_uso.executar(cloudevent("TransacaoAutorizada", tipo_operacao="DEBITO"))

    assert desfecho is Desfecho.CONTABILIZADO
    (lancamento,) = repositorio.lancamentos
    assert {(p.conta, p.natureza) for p in lancamento.partidas} == {
        (Conta.OPERACOES_DE_CREDITO, Natureza.DEBITO),
        (Conta.OBRIGACOES_COM_ESTABELECIMENTOS, Natureza.CREDITO),
    }


def test_data_contabil_e_a_data_de_sao_paulo(
    caso_de_uso: ContabilizarEvento, repositorio: RepositorioContabilEmMemoria
) -> None:
    caso_de_uso.executar(cloudevent("TransacaoAutorizada", tipo_operacao="DEBITO"))

    assert repositorio.lancamentos[0].data_contabil == date(2026, 10, 20)


def test_reserva_vai_para_contas_de_compensacao(
    caso_de_uso: ContabilizarEvento, repositorio: RepositorioContabilEmMemoria
) -> None:
    caso_de_uso.executar(cloudevent("TransacaoAutorizada", tipo_operacao="RESERVA"))

    assert set(repositorio.saldos()) == {
        Conta.COMPENSACAO_LIMITES_COMPROMETIDOS,
        Conta.COMPENSACAO_RESPONSABILIDADE_POR_LIMITES,
    }


def test_reentrega_do_mesmo_evento_e_duplicado(
    caso_de_uso: ContabilizarEvento, repositorio: RepositorioContabilEmMemoria
) -> None:
    evento = cloudevent("TransacaoAutorizada", tipo_operacao="DEBITO")
    caso_de_uso.executar(evento)

    assert caso_de_uso.executar(evento) is Desfecho.DUPLICADO
    assert len(repositorio.lancamentos) == 1


def test_evento_sem_regra_contabil_e_ignorado(caso_de_uso: ContabilizarEvento) -> None:
    recusa = cloudevent("TransacaoRecusada", tipo_operacao="DEBITO", motivo="LIMITE_INSUFICIENTE")
    assert caso_de_uso.executar(recusa) is Desfecho.IGNORADO


def test_cloudevent_incompleto_e_invalido(caso_de_uso: ContabilizarEvento) -> None:
    with pytest.raises(EventoInvalido):
        caso_de_uso.executar({"id": "x", "source": "s"})


def test_campo_desconhecido_nao_quebra_leitor_tolerante(caso_de_uso: ContabilizarEvento) -> None:
    evento = cloudevent("TransacaoAutorizada", tipo_operacao="DEBITO", campo_novo_v1_1="ok")
    assert caso_de_uso.executar(evento) is Desfecho.CONTABILIZADO


def test_id_do_lancamento_e_deterministico() -> None:
    evento = EventoRecebido.de_cloudevent(cloudevent("TransacaoAutorizada", tipo_operacao="DEBITO"))
    assert Lancamento.id_para(evento.source, evento.id) == Lancamento.id_para(
        evento.source, evento.id
    )


@st.composite
def fluxos(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Autorizações (débito/reserva) e, para algumas, o estorno — com reentregas duplicadas."""
    eventos: list[dict[str, Any]] = []
    for _ in range(draw(st.integers(1, 8))):
        tipo = draw(st.sampled_from(["DEBITO", "RESERVA"]))
        valor = str(Decimal(draw(st.integers(1, 100_000))) / 100)
        aut = str(uuid.uuid4())
        eventos.append(
            cloudevent("TransacaoAutorizada", valor, tipo_operacao=tipo, id_autorizacao=aut)
        )
        if draw(st.booleans()):
            eventos.append(
                cloudevent(
                    "TransacaoEstornada",
                    valor,
                    tipo_operacao_original=tipo,
                    id_autorizacao_original=aut,
                )
            )
    duplicatas = draw(st.lists(st.sampled_from(eventos), max_size=5))
    return eventos + duplicatas


@settings(max_examples=150)
@given(eventos=fluxos(), embaralhar=st.randoms(use_true_random=False))
def test_saldo_final_independe_da_ordem_e_das_reentregas(
    eventos: list[dict[str, Any]], embaralhar: Any
) -> None:
    em_ordem = RepositorioContabilEmMemoria()
    for evento in eventos:
        ContabilizarEvento(em_ordem, LivroDeRegras()).executar(evento)

    fora_de_ordem = RepositorioContabilEmMemoria()
    embaralhados = list(eventos)
    embaralhar.shuffle(embaralhados)
    for evento in embaralhados:
        ContabilizarEvento(fora_de_ordem, LivroDeRegras()).executar(evento)

    assert em_ordem.saldos() == fora_de_ordem.saldos()
    assert len(em_ordem.lancamentos) == len({(e["source"], e["id"]) for e in eventos})
