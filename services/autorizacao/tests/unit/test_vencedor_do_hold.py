"""F7-1 (painel 7): a captura/estorno que VENCE a disputa pelo hold nunca termina sem rastro.

Corrida reproduzida pelo juiz: reserva vencida há mais que a TOLERANCIA_DA_DISPUTA; a captura (ou o estorno)
remove o hold no limite e, antes de concluir, o expirador recebe RESERVA_NAO_ENCONTRADA e põe a reserva na
fila humana (RESERVA_SEM_HOLD). Antes: a conclusão falhava na condição "original ainda APROVADA" e a captura
virava RECUSADA 409, sem TransacaoAutorizada/ReservaCapturada, com o limite já movido. Agora o vencedor encerra
a reserva que está na fila por falta de hold (pela 1ª execução, pela retomada do cliente ou pelo reconciliador).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao, Contexto
from autorizacao.application.expirar import (
    TOLERANCIA_DA_DISPUTA,
    ExpirarReservas,
    ResultadoDaExpiracao,
)
from autorizacao.application.portas import (
    ConflitoDeEstado,
    DesfechoNoLimite,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    OriginalJaEncerrada,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.application.reconciliar import GRACA, ReconciliarOrfas, ResultadoDaReconciliacao
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    VALIDADE_DA_RESERVA,
    Autorizacao,
    MotivoDaReconciliacao,
    MotivoRecusa,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.idempotencia import EscopoDeIdempotencia
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

INICIO = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# O expirador só encaminha por falta de hold depois da tolerância: é aí que a corrida existe.
ATRASADO = INICIO + VALIDADE_DA_RESERVA + TOLERANCIA_DA_DISPUTA + timedelta(minutes=1)
CTX = Contexto("corr-1")


@dataclass
class Relogio:
    instante: datetime = INICIO

    def agora(self) -> datetime:
        return self.instante


@dataclass
class Ids:
    _c: count[int] = field(default_factory=count)

    def novo_id(self) -> str:
        return f"ev-{next(self._c)}"


@dataclass
class Limite:
    """Hold único por reserva; quem o remove primeiro vence, os outros recebem RESERVA_NAO_ENCONTRADA (como o
    BC Limites). `no_meio` roda UMA vez, logo depois de uma operação remover o hold e antes de a resposta
    voltar: é a janela em que o expirador atrasado chega."""

    holds: dict[str, Decimal] = field(default_factory=dict)
    utilizado: Decimal = Decimal("0.00")
    aplicadas: set[str] = field(default_factory=set)
    fora: bool = False
    no_meio: Callable[[], object] | None = None

    def aplicar(self, id_contrato: str, op: OperacaoNoLimite) -> ResultadoNoLimite:
        if self.fora:
            raise LimitesIndisponivel("fora")
        if op.id_operacao in self.aplicadas:
            return self._r(DesfechoNoLimite.JA_APLICADA)
        if op.tipo is TipoNoLimite.RESERVAR:
            self.holds[op.id_operacao] = op.valor
        elif op.tipo in (TipoNoLimite.CAPTURAR_RESERVA, TipoNoLimite.LIBERAR_RESERVA):
            if op.id_reserva not in self.holds:
                return self._r(DesfechoNoLimite.RECUSADA, "RESERVA_NAO_ENCONTRADA")
            del self.holds[op.id_reserva]
            if op.tipo is TipoNoLimite.CAPTURAR_RESERVA:
                self.utilizado += op.valor
        self.aplicadas.add(op.id_operacao)  # gravado: daqui em diante a guarda responde JA_APLICADA
        if self.no_meio is not None and op.tipo is not TipoNoLimite.RESERVAR:
            gancho, self.no_meio = self.no_meio, None
            gancho()
        return self._r(DesfechoNoLimite.APLICADA)

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        return EstadoNoLimite(id_operacao in self.aplicadas, id_operacao in self.holds)

    def _r(self, desfecho: DesfechoNoLimite, motivo: str | None = None) -> ResultadoNoLimite:
        reservado = sum(self.holds.values(), Decimal("0.00"))
        saldos = SaldosDoLimite(
            Decimal("1000.00"),
            self.utilizado,
            reservado,
            Decimal("1000.00") - self.utilizado - reservado,
            1,
        )
        return ResultadoNoLimite(desfecho, saldos, motivo)


@dataclass
class Ambiente:
    repo: RepositorioDeAutorizacoesEmMemoria = field(
        default_factory=RepositorioDeAutorizacoesEmMemoria
    )
    limite: Limite = field(default_factory=Limite)
    relogio: Relogio = field(default_factory=Relogio)
    ids: Ids = field(default_factory=Ids)

    @property
    def autorizar(self) -> AutorizarTransacao:
        return AutorizarTransacao(self.repo, self.limite, self.relogio, self.ids)

    @property
    def expirador(self) -> ExpirarReservas:
        return ExpirarReservas(self.repo, self.limite, self.relogio, self.ids)

    @property
    def reconciliador(self) -> ReconciliarOrfas:
        return ReconciliarOrfas(self.repo, self.limite, self.relogio, self.autorizar)

    def reserva(self) -> Autorizacao:
        return self.autorizar.executar(
            pedido(TipoOperacao.RESERVA), escopo("k-res"), CTX
        ).autorizacao

    def morre_antes_de_concluir(self, p: PedidoDeAutorizacao, chave: str) -> None:
        """O limite aplicou e o processo morreu antes do `concluir` (commit incerto)."""
        concluir = self.repo.concluir

        def morre(*_: object) -> None:
            raise RuntimeError("processo morreu")

        self.repo.concluir = morre  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            self.autorizar.executar(p, escopo(chave), CTX)
        self.repo.concluir = concluir  # type: ignore[method-assign]

    def tipos_no_outbox(self) -> list[str]:
        return [type(e).__name__ for e in self.repo.outbox]


def pedido(tipo: TipoOperacao, original: str | None = None) -> PedidoDeAutorizacao:
    return PedidoDeAutorizacao(
        "CTR-1", "0001-1", Decimal("400.00"), "BRL", tipo, id_autorizacao_original=original
    )


def escopo(chave: str) -> EscopoDeIdempotencia:
    return EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", chave)


def captura(id_reserva: str) -> PedidoDeAutorizacao:
    return pedido(TipoOperacao.DEBITO, id_reserva)


def estorno(id_reserva: str) -> PedidoDeAutorizacao:
    return pedido(TipoOperacao.ESTORNO, id_reserva)


@pytest.fixture
def amb() -> Ambiente:
    return Ambiente()


# --- A corrida do juiz, na 1ª execução ---------------------------------------------------------------


def test_captura_que_vence_o_hold_conclui_mesmo_com_o_expirador_no_meio(amb: Ambiente) -> None:
    reserva = amb.reserva()
    amb.relogio.instante = ATRASADO
    rodada: list[object] = []
    amb.limite.no_meio = lambda: rodada.append(amb.expirador.executar())

    desfecho = amb.autorizar.executar(captura(reserva.id_autorizacao), escopo("k-cap"), CTX)

    assert rodada == [{ResultadoDaExpiracao.ENCAMINHADA: 1}]  # o expirador encaminhou de fato
    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA  # 201, não 409
    assert not desfecho.replay
    assert (amb.limite.utilizado, amb.limite.holds) == (Decimal("400.00"), {})
    gravada = amb.repo.obter(reserva.id_autorizacao)
    assert gravada is not None and gravada.status is StatusAutorizacao.CAPTURADA
    assert gravada.motivo_da_reconciliacao is None
    assert amb.repo.fila_humana() == []  # o vencedor apareceu: nada para gente decidir
    assert amb.tipos_no_outbox()[-2:] == ["TransacaoAutorizada", "ReservaCapturada"]


def test_estorno_que_libera_o_hold_conclui_mesmo_com_o_expirador_no_meio(amb: Ambiente) -> None:
    reserva = amb.reserva()
    amb.relogio.instante = ATRASADO
    amb.limite.no_meio = amb.expirador.executar

    desfecho = amb.autorizar.executar(estorno(reserva.id_autorizacao), escopo("k-est"), CTX)

    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA
    assert amb.limite.holds == {}
    gravada = amb.repo.obter(reserva.id_autorizacao)
    assert gravada is not None and gravada.status is StatusAutorizacao.ESTORNADA
    assert amb.repo.fila_humana() == []
    assert amb.tipos_no_outbox()[-1] == "TransacaoEstornada"


# --- O vencedor que caiu antes de concluir: retomada do cliente e reconciliador -------------------------


def test_captura_aplicada_e_retomada_pelo_cliente_depois_do_expirador(amb: Ambiente) -> None:
    reserva = amb.reserva()
    amb.relogio.instante = ATRASADO
    amb.morre_antes_de_concluir(captura(reserva.id_autorizacao), "k-cap")
    assert amb.expirador.executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}
    amb.relogio.instante += DURACAO_DO_LEASE + timedelta(seconds=1)

    desfecho = amb.autorizar.executar(captura(reserva.id_autorizacao), escopo("k-cap"), CTX)

    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA
    assert amb.limite.utilizado == Decimal("400.00")  # JA_APLICADA: capturou uma vez só
    gravada = amb.repo.obter(reserva.id_autorizacao)
    assert gravada is not None and gravada.status is StatusAutorizacao.CAPTURADA
    assert amb.tipos_no_outbox()[-2:] == ["TransacaoAutorizada", "ReservaCapturada"]


@pytest.mark.parametrize(
    ("montar", "status_da_reserva", "ultimo_evento"),
    [
        (captura, StatusAutorizacao.CAPTURADA, "ReservaCapturada"),
        (estorno, StatusAutorizacao.ESTORNADA, "TransacaoEstornada"),  # passa pela marca de estorno
    ],
    ids=["captura", "estorno"],
)
def test_vencedor_aplicado_e_concluido_pelo_reconciliador_depois_do_expirador(
    amb: Ambiente,
    montar: Callable[[str], PedidoDeAutorizacao],
    status_da_reserva: StatusAutorizacao,
    ultimo_evento: str,
) -> None:
    reserva = amb.reserva()
    amb.relogio.instante = ATRASADO
    amb.morre_antes_de_concluir(montar(reserva.id_autorizacao), "k-venc")
    assert amb.expirador.executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}
    amb.relogio.instante += DURACAO_DO_LEASE + GRACA + timedelta(seconds=1)

    assert amb.reconciliador.executar() == {ResultadoDaReconciliacao.CONCLUIDA: 1}

    vencedor = amb.repo.obter(escopo("k-venc").id_autorizacao())
    assert vencedor is not None and vencedor.status is StatusAutorizacao.APROVADA
    gravada = amb.repo.obter(reserva.id_autorizacao)
    assert gravada is not None and gravada.status is status_da_reserva
    assert amb.repo.fila_humana() == []
    assert amb.tipos_no_outbox()[-1] == ultimo_evento


def test_retomada_que_nao_chegou_ao_limite_e_recusada_pelo_limite_e_a_reserva_fica_na_fila(
    amb: Ambiente,
) -> None:
    """A retomada passa pela reserva na fila, mas quem decide é o limite: sem efeito próprio, ela perde."""
    reserva = amb.reserva()
    amb.relogio.instante = ATRASADO
    amb.limite.fora = True
    with pytest.raises(LimitesIndisponivel):  # registrada, nunca chegou ao limite
        amb.autorizar.executar(captura(reserva.id_autorizacao), escopo("k-cap"), CTX)
    amb.limite.fora = False
    del amb.limite.holds[reserva.id_autorizacao]  # expiração interrompida levou o hold
    assert amb.expirador.executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}
    amb.relogio.instante += DURACAO_DO_LEASE + timedelta(seconds=1)

    retomada = amb.autorizar.executar(captura(reserva.id_autorizacao), escopo("k-cap"), CTX)

    assert retomada.autorizacao.motivo_recusa is MotivoRecusa.RESERVA_NAO_ENCONTRADA
    assert amb.limite.utilizado == Decimal("0.00")
    (na_fila,) = amb.repo.fila_humana()
    assert na_fila.motivo_da_reconciliacao is MotivoDaReconciliacao.RESERVA_SEM_HOLD


@pytest.mark.parametrize("montar", [captura, estorno], ids=["captura", "estorno"])
def test_operacao_nova_sobre_reserva_na_fila_continua_409(
    amb: Ambiente, montar: Callable[[str], PedidoDeAutorizacao]
) -> None:
    reserva = amb.reserva()
    del amb.limite.holds[reserva.id_autorizacao]
    amb.relogio.instante = ATRASADO
    assert amb.expirador.executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}

    with pytest.raises(ConflitoDeEstado):
        amb.autorizar.executar(montar(reserva.id_autorizacao), escopo("k-nova"), CTX)


# --- Original encerrada por outro depois do efeito --------------------------------------------------


def _forcar_status(amb: Ambiente, id_autorizacao: str, status: StatusAutorizacao) -> None:
    """Escreve direto no repositório: simula o que o fluxo normal não produz (outro dono da original)."""
    gravada = amb.repo.obter(id_autorizacao)
    assert gravada is not None
    amb.repo._itens[id_autorizacao] = replace(gravada, status=status)


def test_estorno_cuja_original_outro_estorno_encerrou_e_recusado_sem_efeito_proprio(
    amb: Ambiente,
) -> None:
    """Efeito compartilhado: o id do estorno é derivado da original; o TransacaoEstornada é de quem encerrou."""
    reserva = amb.reserva()
    amb.limite.no_meio = lambda: _forcar_status(
        amb, reserva.id_autorizacao, StatusAutorizacao.ESTORNADA
    )

    desfecho = amb.autorizar.executar(estorno(reserva.id_autorizacao), escopo("k-est"), CTX)

    assert desfecho.autorizacao.motivo_recusa is MotivoRecusa.ORIGINAL_JA_ENCERRADA


def test_captura_aplicada_com_original_encerrada_por_outro_fica_em_curso_e_nao_vira_recusa(
    amb: Ambiente, caplog: pytest.LogCaptureFixture
) -> None:
    """Efeito exclusivo com o invariante quebrado: recusar apagaria o efeito; fica em curso, com log."""
    reserva = amb.reserva()
    amb.limite.no_meio = lambda: _forcar_status(
        amb, reserva.id_autorizacao, StatusAutorizacao.EXPIRADA
    )

    with (
        caplog.at_level(logging.ERROR, logger="autorizacao.application.autorizar"),
        pytest.raises(OriginalJaEncerrada),
    ):
        amb.autorizar.executar(captura(reserva.id_autorizacao), escopo("k-cap"), CTX)

    gravada = amb.repo.obter(escopo("k-cap").id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.EM_PROCESSAMENTO
    assert "TransacaoRecusada" not in amb.tipos_no_outbox()
    (linha,) = [json.loads(r.getMessage()) for r in caplog.records]
    assert linha["evento"] == "efeito_aplicado_com_original_encerrada"
    assert linha["status_da_original"] == "EXPIRADA"
