"""Reconciliador de órfãs (garantir + desfazer, roll forward, prazo) e expirador de reservas."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
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
    ArmazenamentoIndisponivel,
    ConflitoDeEstado,
    ContratoInexistente,
    DesfechoNoLimite,
    EmReconciliacao,
    EncerradaSemResultado,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.application.reconciliar import (
    GRACA,
    ReconciliarOrfas,
    ResultadoDaReconciliacao,
)
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    PRAZO_DE_REAPLICACAO,
    VALIDADE_DA_RESERVA,
    Autorizacao,
    MotivoDaReconciliacao,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import (
    ReservaCapturada,
    ReservaExpirada,
    SaldosDoLimite,
    TransacaoAutorizada,
    TransacaoCompensada,
    TransacaoEmReconciliacaoManual,
)
from autorizacao.domain.idempotencia import EscopoDeIdempotencia
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

INICIO = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
DEPOIS_DA_GRACA = DURACAO_DO_LEASE + GRACA + timedelta(seconds=1)


@dataclass
class Relogio:
    instante: datetime = INICIO

    def agora(self) -> datetime:
        return self.instante


@dataclass
class Ids:
    c: count[int] = field(default_factory=count)

    def novo_id(self) -> str:
        return f"ev-{next(self.c)}"


@dataclass
class LimiteFalso:
    """Limite de um contrato com a semântica do BC Limites real que o reconciliador depende:
    guarda por id que ESQUECE (`esquecer_guarda`), idempotência por hold (RESERVAR com hold vivo é
    JA_APLICADA mesmo sem guarda, ANTES de olhar o disponível) e consulta só de leitura."""

    disponivel: Decimal = Decimal("1000.00")
    aplicadas: set[str] = field(default_factory=set)  # a guarda (5 min no real)
    holds: dict[str, Decimal] = field(default_factory=dict)
    fora: bool = False
    fora_para: set[TipoNoLimite] = field(default_factory=set)  # falha só nestes tipos
    chamadas: int = 0  # escritas (aplicar)
    operacoes: list[OperacaoNoLimite] = field(default_factory=list)  # o que foi pedido, na ordem
    consultas: int = 0  # leituras (consultar_operacao)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def aplicar(self, id_contrato: str, op: OperacaoNoLimite) -> ResultadoNoLimite:
        self.chamadas += 1
        self.operacoes.append(op)
        if self.fora or op.tipo in self.fora_para:
            raise LimitesIndisponivel("fora")
        if id_contrato != "CTR-1":
            raise ContratoInexistente(id_contrato)
        with self._lock:
            if op.id_operacao in self.aplicadas:
                return self._r(DesfechoNoLimite.JA_APLICADA)
            if op.tipo is TipoNoLimite.RESERVAR and op.id_operacao in self.holds:
                return self._r(
                    DesfechoNoLimite.JA_APLICADA
                )  # precedência do real: hold antes de saldo
            if op.tipo in (TipoNoLimite.RESERVAR, TipoNoLimite.DEBITAR):
                if self.disponivel < op.valor:
                    return self._r(DesfechoNoLimite.RECUSADA, "LIMITE_INSUFICIENTE")
                self.disponivel -= op.valor
                if op.tipo is TipoNoLimite.RESERVAR:
                    self.holds[op.id_operacao] = op.valor
            elif op.tipo in (TipoNoLimite.LIBERAR_RESERVA, TipoNoLimite.CAPTURAR_RESERVA):
                reservado = self.holds.pop(op.id_reserva or "", None)
                if reservado is None:  # outro levou o hold (como o BC Limites real)
                    return self._r(DesfechoNoLimite.RECUSADA, "RESERVA_NAO_ENCONTRADA")
                capturado = op.valor if op.tipo is TipoNoLimite.CAPTURAR_RESERVA else Decimal(0)
                self.disponivel += reservado - capturado
            else:
                self.disponivel += op.valor
            self.aplicadas.add(op.id_operacao)
            return self._r(DesfechoNoLimite.APLICADA)

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        self.consultas += 1
        if self.fora:
            raise LimitesIndisponivel("fora")
        if id_contrato != "CTR-1":
            raise ContratoInexistente(id_contrato)
        return EstadoNoLimite(id_operacao in self.aplicadas, id_operacao in self.holds)

    def esquecer_guarda(self) -> None:
        """A guarda do limite dura 5 min: depois disso o id some, mas o hold (se houver) fica."""
        self.aplicadas.clear()

    def _r(self, desfecho: DesfechoNoLimite, motivo: str | None = None) -> ResultadoNoLimite:
        saldos = SaldosDoLimite(Decimal(1000), Decimal(0), Decimal(0), self.disponivel, 1)
        return ResultadoNoLimite(desfecho, saldos, motivo)


@pytest.fixture
def ambiente() -> tuple[
    RepositorioDeAutorizacoesEmMemoria, LimiteFalso, Relogio, AutorizarTransacao, ReconciliarOrfas
]:
    repo, limite, relogio = RepositorioDeAutorizacoesEmMemoria(), LimiteFalso(), Relogio()
    autorizar = AutorizarTransacao(repo, limite, relogio, Ids())
    return repo, limite, relogio, autorizar, ReconciliarOrfas(repo, limite, relogio, autorizar)


def pedido(
    tipo: TipoOperacao, valor: str = "300.00", original: str | None = None
) -> PedidoDeAutorizacao:
    return PedidoDeAutorizacao(
        "CTR-1", "0001-1", Decimal(valor), "BRL", tipo, id_autorizacao_original=original
    )


ESCOPO = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "k-1")


def orfa(
    repo: RepositorioDeAutorizacoesEmMemoria,
    autorizar: AutorizarTransacao,
    tipo: TipoOperacao,
    chegou_no_limite: bool,
    original: str | None = None,
) -> None:
    """Simula o processo morrendo: depois de aplicar no limite (ou antes) e antes de concluir."""
    concluir_original = repo.concluir

    def morre(*_: object) -> None:
        raise RuntimeError("processo morreu")

    repo.concluir = morre  # type: ignore[method-assign]
    limites = autorizar.limites
    if not chegou_no_limite:
        autorizar_sem_limite = AutorizarTransacao(
            repo, _QuebraAntes(), autorizar.relogio, autorizar.ids
        )
        with pytest.raises(RuntimeError):
            autorizar_sem_limite.executar(pedido(tipo, original=original), ESCOPO, Contexto("corr"))
    else:
        with pytest.raises(RuntimeError):
            AutorizarTransacao(repo, limites, autorizar.relogio, autorizar.ids).executar(
                pedido(tipo, original=original), ESCOPO, Contexto("corr")
            )
    repo.concluir = concluir_original  # type: ignore[method-assign]


class _QuebraAntes:
    def aplicar(self, id_contrato: str, op: OperacaoNoLimite) -> ResultadoNoLimite:
        raise RuntimeError("processo morreu antes de chamar o limite")


@pytest.mark.parametrize("chegou_no_limite", [True, False], ids=["aplicada", "nao-aplicada"])
@pytest.mark.parametrize("tipo", [TipoOperacao.RESERVA, TipoOperacao.DEBITO])
def test_orfa_de_reserva_ou_debito_e_compensada_com_efeito_liquido_zero(
    ambiente: tuple,
    tipo: TipoOperacao,
    chegou_no_limite: bool,  # type: ignore[type-arg]
) -> None:
    repo, limite, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, tipo, chegou_no_limite)
    relogio.instante += DEPOIS_DA_GRACA

    contagem = reconciliador.executar()

    assert contagem == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.disponivel == Decimal("1000.00")  # nada sobrou reservado/debitado
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.COMPENSADA
    with pytest.raises(
        EncerradaSemResultado
    ):  # o retry tardio do cliente não "ressuscita" a transação
        autorizar.executar(pedido(tipo), ESCOPO, Contexto("corr"))


def test_orfa_de_credito_e_concluida_com_evento(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    limite.disponivel = Decimal("500.00")  # havia 500 utilizados
    orfa(repo, autorizar, TipoOperacao.CREDITO, chegou_no_limite=True)
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.CONCLUIDA: 1}
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.APROVADA
    assert isinstance(repo.outbox[-1], TransacaoAutorizada)
    assert limite.disponivel == Decimal("800.00")  # crédito aplicado uma vez só


def test_dentro_da_graca_nao_e_tocada(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, _, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, TipoOperacao.RESERVA, chegou_no_limite=True)
    relogio.instante += DURACAO_DO_LEASE + timedelta(seconds=10)

    assert reconciliador.executar() == {}


def test_cliente_que_retoma_primeiro_vence_o_reconciliador(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, TipoOperacao.RESERVA, chegou_no_limite=True)
    relogio.instante += DEPOIS_DA_GRACA
    lista = repo.listar_orfas(relogio.agora() - GRACA, 10)

    autorizar.executar(
        pedido(TipoOperacao.RESERVA), ESCOPO, Contexto("corr")
    )  # retomada do cliente

    assert reconciliador.reconciliar(lista[0]) is ResultadoDaReconciliacao.IGNORADA
    assert limite.disponivel == Decimal("700.00")  # a reserva do cliente ficou de pé


def test_limites_fora_do_ar_deixa_para_a_proxima_rodada(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, TipoOperacao.DEBITO, chegou_no_limite=True)
    relogio.instante += DEPOIS_DA_GRACA
    limite.fora = True

    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}

    limite.fora = False
    relogio.instante += DEPOIS_DA_GRACA
    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.disponivel == Decimal("1000.00")


# --- Prazo de reaplicação e captura ---------------------------------------------------------------


def test_orfa_fora_do_prazo_vai_para_reconciliacao_manual_sem_tocar_o_limite(
    ambiente: tuple,  # type: ignore[type-arg]
) -> None:
    repo, limite, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, TipoOperacao.DEBITO, chegou_no_limite=True)
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO + timedelta(seconds=1)  # reconciliador parado

    assert reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}
    assert limite.disponivel == Decimal("700.00")  # nem garantiu nem desfez: decisão humana
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.FORA_DO_PRAZO
    assert repo.listar_orfas(relogio.agora(), 10) == []  # saiu do caminho automático
    with pytest.raises(EmReconciliacao):  # o retry do cliente também não reenvia
        autorizar.executar(pedido(TipoOperacao.DEBITO), ESCOPO, Contexto("corr"))


@pytest.mark.parametrize("chegou_no_limite", [True, False], ids=["aplicada", "nao-aplicada"])
def test_orfa_de_captura_e_concluida_roll_forward(
    ambiente: tuple,  # type: ignore[type-arg]
    chegou_no_limite: bool,
) -> None:
    repo, limite, relogio, autorizar, reconciliador = ambiente
    escopo_reserva = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "r")
    reserva = autorizar.executar(
        pedido(TipoOperacao.RESERVA, "400.00"), escopo_reserva, Contexto("corr")
    ).autorizacao
    orfa(repo, autorizar, TipoOperacao.DEBITO, chegou_no_limite, original=reserva.id_autorizacao)
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.CONCLUIDA: 1}
    captura = repo.obter(ESCOPO.id_autorizacao())
    original = repo.obter(reserva.id_autorizacao)
    assert captura is not None and captura.status is StatusAutorizacao.APROVADA
    assert original is not None and original.status is StatusAutorizacao.CAPTURADA
    assert isinstance(repo.outbox[-1], ReservaCapturada)
    assert limite.disponivel == Decimal("700.00")  # capturou 300 do hold de 400, uma vez só


# --- Expirador de reservas ------------------------------------------------------------------------


def _expirador(ambiente: tuple) -> ExpirarReservas:  # type: ignore[type-arg]
    repo, limite, relogio, _, _ = ambiente
    return ExpirarReservas(repo, limite, relogio, Ids())


def _reserva_aprovada(ambiente: tuple, valor: str = "400.00") -> str:  # type: ignore[type-arg]
    _, _, _, autorizar, _ = ambiente
    return str(
        autorizar.executar(
            pedido(TipoOperacao.RESERVA, valor), ESCOPO, Contexto("corr")
        ).autorizacao.id_autorizacao
    )


def test_reserva_vencida_e_expirada_e_o_limite_volta(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, _ = ambiente
    id_reserva = _reserva_aprovada(ambiente)
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)

    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.EXPIRADA: 1}
    gravada = repo.obter(id_reserva)
    assert gravada is not None and gravada.status is StatusAutorizacao.EXPIRADA
    assert isinstance(repo.outbox[-1], ReservaExpirada)
    assert limite.disponivel == Decimal("1000.00")
    assert _expirador(ambiente).executar() == {}  # saiu do índice de vencimentos


def test_reserva_dentro_da_validade_nao_e_tocada(ambiente: tuple) -> None:  # type: ignore[type-arg]
    _, limite, relogio, _, _ = ambiente
    _reserva_aprovada(ambiente)
    relogio.instante = INICIO + VALIDADE_DA_RESERVA - timedelta(seconds=1)

    assert _expirador(ambiente).executar() == {}
    assert limite.disponivel == Decimal("600.00")


def test_expirador_que_perde_o_hold_nao_encerra_a_reserva(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, _ = ambiente
    id_reserva = _reserva_aprovada(ambiente)
    del limite.holds[id_reserva]  # a captura levou o hold e ainda vai concluir
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)

    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.PERDEU_O_HOLD: 1}
    gravada = repo.obter(id_reserva)
    assert gravada is not None and gravada.status is StatusAutorizacao.APROVADA


def test_expirador_que_cai_no_meio_repete_a_mesma_liberacao(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, _ = ambiente
    _reserva_aprovada(ambiente)
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)
    registrar = repo.registrar_expiracao

    def morre(*_: object) -> None:
        raise RuntimeError("processo morreu depois de liberar no limite")

    repo.registrar_expiracao = morre  # type: ignore[method-assign]
    # Falha inesperada no item não derruba o lote: conta como FALHOU e volta na próxima rodada.
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.FALHOU: 1}
    repo.registrar_expiracao = registrar  # type: ignore[method-assign]

    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.EXPIRADA: 1}
    assert limite.disponivel == Decimal("1000.00")  # liberou 400 uma vez, não 800


def test_expirador_com_limites_fora_tenta_na_proxima_rodada(ambiente: tuple) -> None:  # type: ignore[type-arg]
    _, limite, relogio, _, _ = ambiente
    _reserva_aprovada(ambiente)
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)
    limite.fora = True

    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.FALHOU: 1}
    limite.fora = False
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.EXPIRADA: 1}


# --- Compensação como máquina de estados gravada (achados do painel de juízes) --------------------


def _orfa_debito_ou_reserva(ambiente: tuple, tipo: TipoOperacao, chegou: bool) -> None:  # type: ignore[type-arg]
    repo, _, _, autorizar, _ = ambiente
    orfa(repo, autorizar, tipo, chegou_no_limite=chegou)


def test_reserva_com_reversao_falhando_alem_do_prazo_termina_em_zero(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """A reserva aplicou, a reversão falha por minutos e a guarda do limite esquece o id: o hold é durável,
    então a reserva compensa quando o limite volta, mesmo depois do prazo de reaplicação."""
    repo, limite, relogio, _autorizar, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.RESERVA, chegou=True)
    limite.fora_para = {TipoNoLimite.LIBERAR_RESERVA}
    limite.esquecer_guarda()

    for minutos in (1.2, 2.5, 3.8, 6.0):
        relogio.instante = INICIO + timedelta(minutes=minutos)
        assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.COMPENSANDO
    assert gravada.aplicacao_confirmada_em is not None

    limite.fora_para = set()
    relogio.instante = INICIO + timedelta(minutes=8)
    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.disponivel == Decimal("1000.00") and limite.holds == {}


def test_debito_com_reversao_fora_do_prazo_vai_para_a_fila_com_o_progresso(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    limite.fora_para = {TipoNoLimite.ESTORNAR_DEBITO}
    relogio.instante = INICIO + timedelta(minutes=1.2)
    assert reconciliador.executar() == {
        ResultadoDaReconciliacao.FALHOU: 1
    }  # o limite confirmou a aplicação; a reversão falhou

    relogio.instante = (
        INICIO + timedelta(minutes=1.2) + PRAZO_DE_REAPLICACAO + timedelta(seconds=30)
    )
    assert reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}

    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.DESFAZER_FORA_DO_PRAZO
    assert (
        gravada.aplicacao_confirmada_em is not None and gravada.desfazer_desde is not None
    )  # para o runbook
    assert limite.disponivel == Decimal("700.00")  # o débito está aplicado, e o registro diz isso


def test_debito_com_reversao_recusada_vai_para_a_fila(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    aplicar = limite.aplicar

    def recusa_reversao(id_contrato: str, op: OperacaoNoLimite) -> ResultadoNoLimite:
        if op.tipo is TipoNoLimite.ESTORNAR_DEBITO:  # ex.: pagamento no meio
            return limite._r(DesfechoNoLimite.RECUSADA, "CREDITO_EXCEDE_UTILIZADO")
        return aplicar(id_contrato, op)

    limite.aplicar = recusa_reversao  # type: ignore[method-assign]
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.DESFAZER_RECUSADO


def test_cliente_nao_retoma_depois_que_a_compensacao_comecou(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """PoC do painel (M2): o compensar demorava mais que o lease, o cliente retomava e aprovava, e o
    reconciliador desfazia depois: APROVADA com débito zero. COMPENSANDO gravado antes fecha a porta."""
    _repo, limite, relogio, autorizar, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    limite.fora_para = {TipoNoLimite.ESTORNAR_DEBITO}
    relogio.instante += DEPOIS_DA_GRACA
    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    relogio.instante += timedelta(seconds=30)  # lease do reconciliador vencido
    chamadas = limite.chamadas

    with pytest.raises(EncerradaSemResultado):
        autorizar.executar(pedido(TipoOperacao.DEBITO), ESCOPO, Contexto("corr"))

    assert limite.chamadas == chamadas
    limite.fora_para = set()
    relogio.instante += DEPOIS_DA_GRACA
    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.disponivel == Decimal("1000.00")


def test_reserva_fora_do_prazo_ainda_compensa(ambiente: tuple) -> None:  # type: ignore[type-arg]
    _repo, limite, relogio, _autorizar, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.RESERVA, chegou=True)
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO + timedelta(minutes=10)  # reconciliador parado

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.disponivel == Decimal("1000.00") and limite.holds == {}


def test_reserva_vencida_sem_hold_e_sem_vencedor_vai_para_a_fila(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, _ = ambiente
    id_reserva = _reserva_aprovada(ambiente)
    del limite.holds[
        id_reserva
    ]  # o vencedor da disputa nunca concluiu (ex.: está em reconciliação)
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(minutes=5)
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.PERDEU_O_HOLD: 1}

    relogio.instante = INICIO + VALIDADE_DA_RESERVA + TOLERANCIA_DA_DISPUTA + timedelta(minutes=1)
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}
    assert _expirador(ambiente).executar() == {}  # saiu do índice de vencimentos: sem sangria
    (na_fila,) = repo.fila_humana()
    assert na_fila.id_autorizacao == id_reserva
    assert (
        na_fila.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    )  # não finge mais ser reserva viva
    assert na_fila.motivo_da_reconciliacao is MotivoDaReconciliacao.RESERVA_SEM_HOLD
    assert isinstance(repo.outbox[-1], TransacaoEmReconciliacaoManual)


# --- Painel 4: perguntar em vez de aplicar; ramos sem teste; lease relido; eventos ----------------


def test_debito_que_nunca_chegou_ao_limite_compensa_sem_nenhuma_escrita(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """PoC B1 do painel: a 1ª versão aplicava o débito para "garantir" e, se a reversão fosse recusada
    (pagamento no meio), o cliente ficava com um débito fantasma. Agora o reconciliador PERGUNTA."""
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=False)
    escritas = limite.chamadas
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.chamadas == escritas  # nenhuma escrita: só a consulta
    assert limite.consultas == 1
    assert limite.disponivel == Decimal("1000.00")
    evento = repo.outbox[-1]
    assert isinstance(evento, TransacaoCompensada) and evento.havia_efeito is False


def test_reserva_com_hold_vivo_e_sem_saldo_libera_o_hold(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """A1 do painel: hold vivo + disponível zerado por outras compras + guarda esquecida. A decisão vem da
    observação do hold (consulta), não de inferir algo de uma recusa."""
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.RESERVA, chegou=True)
    limite.disponivel = Decimal("0.00")  # outras compras consumiram o resto
    limite.esquecer_guarda()
    relogio.instante = INICIO + timedelta(days=2)  # reconciliador parado por muito tempo

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.holds == {}  # o hold não foi abandonado
    evento = repo.outbox[-1]
    assert isinstance(evento, TransacaoCompensada) and evento.havia_efeito is True


def test_dois_reconciliadores_so_um_assume(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    relogio.instante += DEPOIS_DA_GRACA
    (orfa_vista,) = repo.listar_orfas(relogio.agora() - GRACA, 10)
    outro = ReconciliarOrfas(repo, limite, relogio, autorizar)

    assert outro.reconciliar(orfa_vista) is ResultadoDaReconciliacao.COMPENSADA
    assert (
        reconciliador.reconciliar(orfa_vista) is ResultadoDaReconciliacao.IGNORADA
    )  # perdeu o CAS
    assert limite.disponivel == Decimal("1000.00")


def test_contrato_inexistente_compensa_sem_efeito(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    escopo = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-X/autorizacoes", "k-x")
    repo.concluir = lambda *_: (_ for _ in ()).throw(RuntimeError("morreu"))  # type: ignore[method-assign]
    with pytest.raises(LimitesIndisponivel):
        limite.fora = True
        autorizar.executar(
            PedidoDeAutorizacao("CTR-X", "0001-1", Decimal("10.00"), "BRL", TipoOperacao.DEBITO),
            escopo,
            Contexto("c"),
        )
    limite.fora = False
    del repo.concluir  # volta ao método da classe
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}


def test_roll_forward_fora_do_prazo_vai_para_a_fila(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, autorizar, reconciliador = ambiente
    limite.disponivel = Decimal("500.00")
    orfa(repo, autorizar, TipoOperacao.CREDITO, chegou_no_limite=True)
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO + timedelta(seconds=1)

    assert reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.FORA_DO_PRAZO
    assert isinstance(repo.outbox[-1], TransacaoEmReconciliacaoManual)


def test_roll_forward_com_original_sumida_falha_e_espera_gente(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, _limite, relogio, autorizar, reconciliador = ambiente
    escopo_reserva = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "r")
    reserva = autorizar.executar(
        pedido(TipoOperacao.RESERVA, "400.00"), escopo_reserva, Contexto("c")
    ).autorizacao
    orfa(
        repo,
        autorizar,
        TipoOperacao.DEBITO,
        chegou_no_limite=False,
        original=reserva.id_autorizacao,
    )
    del repo._itens[reserva.id_autorizacao]  # inconsistência que só uma pessoa explica
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}


def test_compensando_de_debito_sem_confirmacao_fora_do_prazo_vai_para_a_fila(
    ambiente: tuple,
) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    relogio.instante += DEPOIS_DA_GRACA
    limite.fora = True  # gravou COMPENSANDO, mas a consulta falhou
    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    gravada = repo.obter(ESCOPO.id_autorizacao())
    assert gravada is not None and gravada.status is StatusAutorizacao.COMPENSANDO
    limite.fora = False
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO + timedelta(seconds=1)

    assert reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}
    assert limite.disponivel == Decimal("700.00")  # nada foi feito às cegas


def test_reversao_de_reserva_ja_liberada_conclui(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """A reversão aplicou e o processo caiu antes de concluir: na volta, LIBERAR responde que o hold não
    existe mais. Só esta compensação remove o hold de autorização não aprovada → COMPENSADA."""
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.RESERVA, chegou=True)
    relogio.instante += DEPOIS_DA_GRACA
    concluir = repo.concluir
    repo.concluir = lambda *_: (_ for _ in ()).throw(RuntimeError("morreu"))  # type: ignore[method-assign]
    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    repo.concluir = concluir  # type: ignore[method-assign]
    limite.esquecer_guarda()  # a guarda já esqueceu o id da reversão
    relogio.instante += DEPOIS_DA_GRACA

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert limite.holds == {} and limite.disponivel == Decimal("1000.00")


class RelogioQueAnda:
    """Cada leitura avança 6 s: simula passos lentos (o PoC A3 do painel)."""

    def __init__(self, inicio: datetime) -> None:
        self.instante = inicio

    def agora(self) -> datetime:
        self.instante += timedelta(seconds=6)
        return self.instante


def test_cada_passo_rele_o_relogio_e_renova_o_lease() -> None:
    repo, limite = RepositorioDeAutorizacoesEmMemoria(), LimiteFalso()
    fixo = Relogio()
    autorizar = AutorizarTransacao(repo, limite, fixo, Ids())
    orfa(repo, autorizar, TipoOperacao.DEBITO, chegou_no_limite=True)
    leases: list[datetime] = []
    regravar = repo.regravar

    def espiar(nova: Autorizacao, anterior: datetime) -> bool:
        leases.append(nova.lease_ate)
        return regravar(nova, anterior)

    repo.regravar = espiar  # type: ignore[method-assign]
    andando = RelogioQueAnda(INICIO + DEPOIS_DA_GRACA)
    reconciliador = ReconciliarOrfas(repo, limite, andando, autorizar)

    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    # assumir, COMPENSANDO, aplicação confirmada, desfazer: cada passo com lease novo, estritamente crescente
    assert len(leases) == 4 and leases == sorted(set(leases))


def test_lease_perdido_no_meio_aborta_sem_tocar_o_limite(ambiente: tuple) -> None:  # type: ignore[type-arg]
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_debito_ou_reserva(ambiente, TipoOperacao.DEBITO, chegou=True)
    relogio.instante += DEPOIS_DA_GRACA
    regravar = repo.regravar
    passos = iter([True, False])  # grava COMPENSANDO; perde o lease ao confirmar a aplicação

    def as_vezes(nova: Autorizacao, anterior: datetime) -> bool:
        return regravar(nova, anterior) if next(passos) else False

    repo.regravar = as_vezes  # type: ignore[method-assign]
    escritas = limite.chamadas

    assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    assert limite.chamadas == escritas  # não desfez nada sem a posse


def test_reserva_em_reconciliacao_manual_nao_e_estornada_nem_capturada(ambiente: tuple) -> None:  # type: ignore[type-arg]
    _repo, limite, relogio, autorizar, _ = ambiente
    id_reserva = _reserva_aprovada(ambiente)
    del limite.holds[id_reserva]
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + TOLERANCIA_DA_DISPUTA + timedelta(minutes=1)
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.ENCAMINHADA: 1}
    estorno = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "est")

    with pytest.raises(ConflitoDeEstado):  # em análise: ninguém mexe até decidirem
        autorizar.executar(
            pedido(TipoOperacao.ESTORNO, "400.00", original=id_reserva), estorno, Contexto("c")
        )


# --- painel 6: uma órfã/reserva envenenada não para o lote; operações automáticas sem id_conta ----------


def _orfa_direta(repo: RepositorioDeAutorizacoesEmMemoria, id_autorizacao: str) -> None:
    """Órfã de RESERVA que nunca chegou ao limite (processo morreu logo depois de registrar)."""
    repo.registrar(
        Autorizacao.iniciar(
            pedido(TipoOperacao.RESERVA),
            id_autorizacao=id_autorizacao,
            impressao_digital="imp",
            client_id="canal",
            agora=INICIO,
            correlation_id="corr",
            traceparent=None,
        )
    )


def test_orfa_envenenada_conta_como_falhou_e_as_outras_seguem(
    ambiente: tuple,  # type: ignore[type-arg]
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, limite, relogio, _, reconciliador = ambiente
    _orfa_direta(repo, "aut-envenenada")
    _orfa_direta(repo, "aut-sadia")
    relogio.instante += DEPOIS_DA_GRACA
    consultar = limite.consultar_operacao

    def envenenada(id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        if id_operacao == "aut-envenenada":
            raise IndexError("list index out of range")  # o que o traceparent "X" fazia
        return consultar(id_contrato, id_operacao)

    limite.consultar_operacao = envenenada  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="autorizacao.application.reconciliar"):
        contagem = reconciliador.executar()

    assert contagem == {ResultadoDaReconciliacao.FALHOU: 1, ResultadoDaReconciliacao.COMPENSADA: 1}
    assert "aut-envenenada" in caplog.text
    sadia = repo.obter("aut-sadia")
    assert sadia is not None and sadia.status is StatusAutorizacao.COMPENSADA


def test_banco_indisponivel_na_orfa_e_falhou_sem_traceback(
    ambiente: tuple,  # type: ignore[type-arg]
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _, relogio, _, reconciliador = ambiente
    _orfa_direta(repo, "aut-1")
    relogio.instante += DEPOIS_DA_GRACA

    def estrangulado(*_: object) -> bool:
        raise ArmazenamentoIndisponivel("regravar")

    repo.regravar = estrangulado  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="autorizacao.application.reconciliar"):
        assert reconciliador.executar() == {ResultadoDaReconciliacao.FALHOU: 1}
    assert caplog.records == []  # falha esperada: entra na contagem FALHOU, não no log de erro


def test_reserva_envenenada_conta_como_falhou_e_as_outras_expiram(
    ambiente: tuple,  # type: ignore[type-arg]
) -> None:
    repo, _, relogio, autorizar, _ = ambiente
    envenenada = _reserva_aprovada(ambiente, "100.00")
    outra = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "k-2")
    autorizar.executar(pedido(TipoOperacao.RESERVA, "200.00"), outra, Contexto("corr"))
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)
    registrar = repo.registrar_expiracao

    def morre_so_numa(expirada: Autorizacao) -> None:
        if expirada.id_autorizacao == envenenada:
            raise RuntimeError("item envenenado")
        registrar(expirada)

    repo.registrar_expiracao = morre_so_numa  # type: ignore[method-assign]

    assert _expirador(ambiente).executar() == {
        ResultadoDaExpiracao.FALHOU: 1,
        ResultadoDaExpiracao.EXPIRADA: 1,
    }


def test_compensacao_e_expiracao_nao_levam_id_conta(ambiente: tuple) -> None:  # type: ignore[type-arg]
    """Só o pedido do canal leva a conta ao limite: desfazer/expirar não pode ser barrado por uma conta que
    mudou depois (portabilidade), senão o hold fica preso."""
    repo, limite, relogio, autorizar, reconciliador = ambiente
    orfa(repo, autorizar, TipoOperacao.RESERVA, chegou_no_limite=True)
    relogio.instante += DEPOIS_DA_GRACA
    assert reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    outra = EscopoDeIdempotencia("canal", "POST", "/v1/contratos/CTR-1/autorizacoes", "k-2")
    autorizar.executar(pedido(TipoOperacao.RESERVA, "100.00"), outra, Contexto("corr"))
    relogio.instante += VALIDADE_DA_RESERVA + timedelta(seconds=1)
    assert _expirador(ambiente).executar() == {ResultadoDaExpiracao.EXPIRADA: 1}

    contas = [(op.tipo, op.id_conta) for op in limite.operacoes]
    assert contas == [
        (TipoNoLimite.RESERVAR, "0001-1"),  # canal
        (TipoNoLimite.LIBERAR_RESERVA, None),  # compensação
        (TipoNoLimite.RESERVAR, "0001-1"),  # canal
        (TipoNoLimite.LIBERAR_RESERVA, None),  # expiração
    ]
