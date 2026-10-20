"""Os dois BCs juntos, em processo, com a guarda de idempotência REAL do limite (que esquece em 5 min).

Os testes unitários do `autorizacao` usam um limite falso que nunca esquece; aqui o `limites` é o de verdade
(domínio + repositório em memória, que compacta a guarda como o DynamoDB). Cada teste mostra primeiro o
perigo (o limite aceita reaplicar um id esquecido) e depois que o `autorizacao` não deixa isso acontecer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao, Contexto
from autorizacao.application.portas import (
    ContratoInexistente,
    DesfechoNoLimite,
    EmReconciliacao,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.application.reconciliar import ReconciliarOrfas, ResultadoDaReconciliacao
from autorizacao.domain.autorizacao import (
    PRAZO_DE_REAPLICACAO,
    MotivoDaReconciliacao,
    MotivoRecusa,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.idempotencia import EscopoDeIdempotencia
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.portas import ContratoNaoEncontrado
from limites.domain.limite import JANELA_DE_DEDUPLICACAO, Limite, TipoProduto
from limites.domain.operacoes import (
    CapturarReserva,
    Creditar,
    Debitar,
    EstornarDebito,
    LiberarReserva,
    OperacaoDeLimite,
    Reservar,
)

INICIO = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
CTR = "CTR-1"


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
class LimitesEmProcesso:
    """GatewayDeLimites que fala direto com o BC Limites real (o que o HTTP interno faria)."""

    repositorio: RepositorioDeLimitesEmMemoria
    relogio: Relogio
    fora: bool = False

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        if self.fora:
            raise LimitesIndisponivel("fora")
        try:
            r = self.repositorio.aplicar(id_contrato, _traduzir(operacao), self.relogio.agora())
        except ContratoNaoEncontrado as erro:
            raise ContratoInexistente(id_contrato) from erro
        s = r.saldos
        return ResultadoNoLimite(
            DesfechoNoLimite(r.desfecho.value),
            SaldosDoLimite(
                s.limite_concedido,
                s.limite_utilizado,
                s.saldo_reservado,
                s.limite_disponivel,
                s.versao,
            ),
            r.motivo.value if r.motivo else None,
        )

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        if self.fora:
            raise LimitesIndisponivel("fora")
        limite = self.repositorio.obter(id_contrato)
        if limite is None:
            raise ContratoInexistente(id_contrato)
        return EstadoNoLimite(limite.ja_aplicou(id_operacao), limite.tem_reserva(id_operacao))


def _traduzir(op: OperacaoNoLimite) -> OperacaoDeLimite:
    match op.tipo:
        case TipoNoLimite.RESERVAR:
            return Reservar(op.id_operacao, op.valor)
        case TipoNoLimite.DEBITAR:
            return Debitar(op.id_operacao, op.valor)
        case TipoNoLimite.CREDITAR:
            return Creditar(op.id_operacao, op.valor)
        case TipoNoLimite.ESTORNAR_DEBITO:
            return EstornarDebito(op.id_operacao, op.valor)
        case TipoNoLimite.LIBERAR_RESERVA:
            return LiberarReserva(op.id_operacao, str(op.id_reserva), op.valor)
        case TipoNoLimite.CAPTURAR_RESERVA:
            assert op.valor_reservado is not None
            return CapturarReserva(op.id_operacao, str(op.id_reserva), op.valor_reservado, op.valor)


@dataclass
class Mundo:
    relogio: Relogio
    limites: RepositorioDeLimitesEmMemoria
    gateway: LimitesEmProcesso
    autorizacoes: RepositorioDeAutorizacoesEmMemoria
    autorizar: AutorizarTransacao
    reconciliador: ReconciliarOrfas

    def utilizado(self) -> Decimal:
        limite = self.limites.obter(CTR)
        assert limite is not None
        return limite.utilizado

    def trafego_no_contrato(self, id_operacao: str) -> None:
        """Qualquer escrita no contrato compacta a guarda (como no DynamoDB)."""
        self.limites.aplicar(CTR, Reservar(id_operacao, Decimal("0.01")), self.relogio.agora())

    def trafego_no_contrato_sem_saldo(self) -> None:
        """Escrita que não depende de saldo (pagamento), para compactar a guarda de um contrato sem disponível."""
        self.limites.aplicar(CTR, Creditar("pagamento", Decimal("0.01")), self.relogio.agora())


@pytest.fixture
def mundo() -> Mundo:
    relogio = Relogio()
    limites = RepositorioDeLimitesEmMemoria()
    limites.criar(Limite.novo(CTR, "0001-1", TipoProduto.CREDITO, Decimal("1000.00")))
    gateway = LimitesEmProcesso(limites, relogio)
    autorizacoes = RepositorioDeAutorizacoesEmMemoria()
    autorizar = AutorizarTransacao(autorizacoes, gateway, relogio, Ids())
    return Mundo(
        relogio,
        limites,
        gateway,
        autorizacoes,
        autorizar,
        ReconciliarOrfas(autorizacoes, gateway, relogio, autorizar),
    )


def escopo(chave: str) -> EscopoDeIdempotencia:
    return EscopoDeIdempotencia("canal", "POST", f"/v1/contratos/{CTR}/autorizacoes", chave)


def pedido(tipo: TipoOperacao, valor: str, original: str | None = None) -> PedidoDeAutorizacao:
    return PedidoDeAutorizacao(
        CTR, "0001-1", Decimal(valor), "BRL", tipo, id_autorizacao_original=original
    )


def morre_ao_concluir(mundo: Mundo) -> None:
    """O processo morre depois de aplicar no limite e antes de gravar a conclusão (uma vez)."""
    concluir = mundo.autorizacoes.concluir

    def morre(*_: object) -> None:
        mundo.autorizacoes.concluir = concluir  # type: ignore[method-assign]
        raise RuntimeError("processo morreu")

    mundo.autorizacoes.concluir = morre  # type: ignore[method-assign]


def test_o_perigo_existe_a_guarda_do_limite_esquece(mundo: Mundo) -> None:
    """Sem as proteções do `autorizacao`, reenviar um DEBITAR depois da janela debita de novo."""
    mundo.gateway.aplicar(CTR, OperacaoNoLimite(TipoNoLimite.DEBITAR, "x", Decimal("100.00")))
    mundo.relogio.instante += JANELA_DE_DEDUPLICACAO + timedelta(seconds=30)
    mundo.trafego_no_contrato("outra")

    reenvio = mundo.gateway.aplicar(
        CTR, OperacaoNoLimite(TipoNoLimite.DEBITAR, "x", Decimal("100.00"))
    )

    assert (
        reenvio.desfecho is DesfechoNoLimite.APLICADA
    )  # aplicou de novo: é contra isso que o resto protege
    assert mundo.utilizado() == Decimal("200.00")


def test_prazo_de_reaplicacao_cabe_na_guarda_com_folga_para_relogio() -> None:
    """As duas constantes moram em BCs diferentes; quem mudar uma precisa mudar a outra (ADR-0006)."""
    assert PRAZO_DE_REAPLICACAO + timedelta(seconds=60) <= JANELA_DE_DEDUPLICACAO


def test_retomada_do_cliente_depois_do_prazo_nao_debita_de_novo(mundo: Mundo) -> None:
    morre_ao_concluir(mundo)
    with pytest.raises(RuntimeError):
        mundo.autorizar.executar(pedido(TipoOperacao.DEBITO, "100.00"), escopo("k"), Contexto("c"))
    mundo.relogio.instante += JANELA_DE_DEDUPLICACAO + timedelta(seconds=30)
    mundo.trafego_no_contrato("outra")

    with pytest.raises(EmReconciliacao):
        mundo.autorizar.executar(pedido(TipoOperacao.DEBITO, "100.00"), escopo("k"), Contexto("c"))

    assert mundo.utilizado() == Decimal("100.00")


def test_reconciliador_atrasado_nao_compensa_debito_as_cegas(mundo: Mundo) -> None:
    morre_ao_concluir(mundo)
    with pytest.raises(RuntimeError):
        mundo.autorizar.executar(pedido(TipoOperacao.DEBITO, "100.00"), escopo("k"), Contexto("c"))
    mundo.relogio.instante += JANELA_DE_DEDUPLICACAO + timedelta(seconds=30)
    mundo.trafego_no_contrato("outra")

    assert mundo.reconciliador.executar() == {ResultadoDaReconciliacao.ENCAMINHADA: 1}
    gravada = mundo.autorizacoes.obter(escopo("k").id_autorizacao())
    assert gravada is not None
    assert gravada.motivo_da_reconciliacao is MotivoDaReconciliacao.FORA_DO_PRAZO
    assert mundo.utilizado() == Decimal("100.00")  # garantir agora debitaria de novo; não garantiu


def test_reserva_orfa_compensa_mesmo_com_a_guarda_esquecida(mundo: Mundo) -> None:
    morre_ao_concluir(mundo)
    with pytest.raises(RuntimeError):
        mundo.autorizar.executar(pedido(TipoOperacao.RESERVA, "300.00"), escopo("r"), Contexto("c"))
    mundo.relogio.instante += JANELA_DE_DEDUPLICACAO + timedelta(minutes=5)
    mundo.trafego_no_contrato("outra")

    assert mundo.reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    limite = mundo.limites.obter(CTR)
    assert limite is not None
    assert limite.reservado == Decimal(
        "0.01"
    )  # só a reserva do tráfego de fundo; o hold da órfã saiu


def test_segundo_estorno_minutos_depois_nao_reaplica_o_id_derivado(mundo: Mundo) -> None:
    debito = mundo.autorizar.executar(
        pedido(TipoOperacao.DEBITO, "300.00"), escopo("d"), Contexto("c")
    ).autorizacao
    estorno = pedido(TipoOperacao.ESTORNO, "300.00", debito.id_autorizacao)
    morre_ao_concluir(mundo)
    with pytest.raises(RuntimeError):  # o 1º estorno aplicou no limite e morreu
        mundo.autorizar.executar(estorno, escopo("e1"), Contexto("c"))
    mundo.relogio.instante += JANELA_DE_DEDUPLICACAO + timedelta(minutes=1)
    mundo.trafego_no_contrato("outra")

    segundo = mundo.autorizar.executar(estorno, escopo("e2"), Contexto("c")).autorizacao

    assert segundo.motivo_recusa is MotivoRecusa.ESTORNO_EM_ANDAMENTO
    assert mundo.utilizado() == Decimal("0.00")  # estornou 300 uma vez, não 600 (que daria −300)
    original = mundo.autorizacoes.obter(debito.id_autorizacao)
    assert original is not None and original.status is StatusAutorizacao.APROVADA


def test_reserva_orfa_com_hold_vivo_sem_saldo_e_guarda_esquecida_libera_o_hold(
    mundo: Mundo,
) -> None:
    """A1 do painel 4 com o limites REAL: hold vivo, disponível zerado por outras compras, guarda
    esquecida, reconciliador dias atrasado. A compensação observa o hold e o libera; não infere nada."""
    morre_ao_concluir(mundo)
    with pytest.raises(RuntimeError):
        mundo.autorizar.executar(pedido(TipoOperacao.RESERVA, "300.00"), escopo("r"), Contexto("c"))
    mundo.limites.aplicar(CTR, Debitar("consome-o-resto", Decimal("700.00")), mundo.relogio.agora())
    mundo.relogio.instante += timedelta(days=2)
    mundo.trafego_no_contrato_sem_saldo()

    assert mundo.reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    limite = mundo.limites.obter(CTR)
    assert limite is not None
    assert limite.reservado == Decimal("0.00") and limite.reservas == {}
    limite.verificar_invariantes()


def test_debito_orfao_que_nao_chegou_nao_ganha_debito_fantasma(mundo: Mundo) -> None:
    """PoC B1 do painel 4: nenhuma escrita para "descobrir"; o cliente não paga por compra não autorizada."""
    mundo.gateway.fora = True
    with pytest.raises(LimitesIndisponivel):
        mundo.autorizar.executar(pedido(TipoOperacao.DEBITO, "300.00"), escopo("d"), Contexto("c"))
    mundo.gateway.fora = False
    mundo.relogio.instante += timedelta(seconds=80)

    assert mundo.reconciliador.executar() == {ResultadoDaReconciliacao.COMPENSADA: 1}
    assert mundo.utilizado() == Decimal("0.00")
