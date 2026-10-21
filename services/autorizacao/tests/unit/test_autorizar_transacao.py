"""Caso de uso AutorizarTransacao com repositório em memória e um BC Limites falso (porta)."""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao, Contexto
from autorizacao.application.portas import (
    AutorizacaoOriginalInvalida,
    ChaveReutilizada,
    ConflitoDeEstado,
    ContratoInexistente,
    DesfechoNoLimite,
    EmAndamento,
    EmReconciliacao,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    PRAZO_DE_REAPLICACAO,
    MotivoRecusa,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import (
    ReservaCapturada,
    SaldosDoLimite,
    TransacaoAutorizada,
    TransacaoEstornada,
    TransacaoRecusada,
)
from autorizacao.domain.idempotencia import EscopoDeIdempotencia, id_operacao_de_estorno
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao

INICIO = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


@dataclass
class RelogioAjustavel:
    instante: datetime = INICIO

    def agora(self) -> datetime:
        return self.instante


@dataclass
class IdsSequenciais:
    _contador: count[int] = field(default_factory=count)

    def novo_id(self) -> str:
        return f"ev-{next(self._contador)}"


@dataclass
class LimitesFalso:
    """BC Limites simplificado: idempotente por id_operacao, recusa quando não cabe."""

    disponivel: Decimal = Decimal("1000.00")
    contratos: set[str] = field(default_factory=lambda: {"CTR-1"})
    indisponivel: bool = False
    barreira: threading.Barrier | None = None
    aplicadas: dict[str, OperacaoNoLimite] = field(default_factory=dict)
    chamadas: list[OperacaoNoLimite] = field(default_factory=list)
    holds: dict[str, Decimal] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _versao: int = 1

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        self.chamadas.append(operacao)
        if self.barreira is not None:
            self.barreira.wait(timeout=5)
        if self.indisponivel:
            raise LimitesIndisponivel("circuit breaker aberto")
        if id_contrato not in self.contratos:
            raise ContratoInexistente(id_contrato)
        with self._lock:
            if operacao.id_operacao in self.aplicadas:
                return ResultadoNoLimite(DesfechoNoLimite.JA_APLICADA, self._saldos())
            if operacao.tipo in (TipoNoLimite.LIBERAR_RESERVA, TipoNoLimite.CAPTURAR_RESERVA):
                if (operacao.id_reserva or "") not in self.holds:  # outro levou o hold
                    return ResultadoNoLimite(
                        DesfechoNoLimite.RECUSADA, self._saldos(), "RESERVA_NAO_ENCONTRADA"
                    )
                reservado = self.holds.pop(operacao.id_reserva or "")
                capturado = operacao.valor if operacao.tipo is TipoNoLimite.CAPTURAR_RESERVA else 0
                self.disponivel += reservado - capturado
            elif operacao.tipo in (TipoNoLimite.RESERVAR, TipoNoLimite.DEBITAR):
                if self.disponivel < operacao.valor:
                    return ResultadoNoLimite(
                        DesfechoNoLimite.RECUSADA, self._saldos(), "LIMITE_INSUFICIENTE"
                    )
                self.disponivel -= operacao.valor
                if operacao.tipo is TipoNoLimite.RESERVAR:
                    self.holds[operacao.id_operacao] = operacao.valor
            else:
                self.disponivel += operacao.valor
            self.aplicadas[operacao.id_operacao] = operacao
            self._versao += 1
            return ResultadoNoLimite(DesfechoNoLimite.APLICADA, self._saldos())

    def _saldos(self) -> SaldosDoLimite:
        reservado = Decimal("1000.00") - self.disponivel
        return SaldosDoLimite(
            Decimal("1000.00"), Decimal("0.00"), reservado, self.disponivel, self._versao
        )


@pytest.fixture
def relogio() -> RelogioAjustavel:
    return RelogioAjustavel()


@pytest.fixture
def repositorio() -> RepositorioDeAutorizacoesEmMemoria:
    return RepositorioDeAutorizacoesEmMemoria()


@pytest.fixture
def limites() -> LimitesFalso:
    return LimitesFalso()


@pytest.fixture
def caso_de_uso(
    repositorio: RepositorioDeAutorizacoesEmMemoria,
    limites: LimitesFalso,
    relogio: RelogioAjustavel,
) -> AutorizarTransacao:
    return AutorizarTransacao(repositorio, limites, relogio, IdsSequenciais())


def pedido(
    tipo: TipoOperacao = TipoOperacao.RESERVA, valor: str = "150.00", **extra: str
) -> PedidoDeAutorizacao:
    return PedidoDeAutorizacao("CTR-1", "0001-1", Decimal(valor), "BRL", tipo, **extra)  # type: ignore[arg-type]


def escopo(chave: str = "chave-1", contrato: str = "CTR-1") -> EscopoDeIdempotencia:
    return EscopoDeIdempotencia(
        "canal-pos", "POST", f"/v1/contratos/{contrato}/autorizacoes", chave
    )


CTX = Contexto(correlation_id="corr-1")


def test_reserva_aprovada_grava_estado_e_evento(
    caso_de_uso: AutorizarTransacao, repositorio: RepositorioDeAutorizacoesEmMemoria
) -> None:
    desfecho = caso_de_uso.executar(pedido(), escopo(), CTX)

    assert not desfecho.replay
    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA
    assert desfecho.autorizacao.saldos is not None
    assert desfecho.autorizacao.saldos.saldo_reservado == Decimal("150.00")
    (evento,) = repositorio.outbox
    assert isinstance(evento, TransacaoAutorizada)


def test_replay_devolve_o_original_sem_chamar_o_limite_de_novo(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    primeira = caso_de_uso.executar(pedido(), escopo(), CTX)

    replay = caso_de_uso.executar(pedido(valor="150"), escopo(), CTX)  # mesmo conteúdo semântico

    assert replay.replay
    assert replay.autorizacao.id_autorizacao == primeira.autorizacao.id_autorizacao
    assert len(limites.chamadas) == 1


def test_mesma_chave_com_pedido_diferente_e_rejeitada(caso_de_uso: AutorizarTransacao) -> None:
    caso_de_uso.executar(pedido(), escopo(), CTX)

    with pytest.raises(ChaveReutilizada):
        caso_de_uso.executar(pedido(valor="151.00"), escopo(), CTX)


def test_mesma_chave_em_contrato_diferente_nao_colide(caso_de_uso: AutorizarTransacao) -> None:
    caso_de_uso.executar(pedido(), escopo(), CTX)
    outro = PedidoDeAutorizacao("CTR-2", "0001-1", Decimal("1.00"), "BRL", TipoOperacao.DEBITO)

    desfecho = caso_de_uso.executar(outro, escopo(contrato="CTR-2"), CTX)

    assert desfecho.autorizacao.motivo_recusa is MotivoRecusa.CONTRATO_NAO_ENCONTRADO


def test_limite_insuficiente_e_recusa_registrada_e_publicada(
    caso_de_uso: AutorizarTransacao, repositorio: RepositorioDeAutorizacoesEmMemoria
) -> None:
    desfecho = caso_de_uso.executar(pedido(valor="1000.01"), escopo(), CTX)

    assert desfecho.autorizacao.motivo_recusa is MotivoRecusa.LIMITE_INSUFICIENTE
    assert isinstance(repositorio.outbox[0], TransacaoRecusada)
    replay = caso_de_uso.executar(pedido(valor="1000.01"), escopo(), CTX)
    assert replay.replay
    assert replay.autorizacao.motivo_recusa is MotivoRecusa.LIMITE_INSUFICIENTE


def test_limites_fora_do_ar_deixa_retomavel_e_o_retry_conclui_uma_vez(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, relogio: RelogioAjustavel
) -> None:
    limites.indisponivel = True
    with pytest.raises(LimitesIndisponivel):
        caso_de_uso.executar(pedido(), escopo(), CTX)

    with pytest.raises(EmAndamento):  # dentro do lease: outra execução "ainda" é a dona
        caso_de_uso.executar(pedido(), escopo(), CTX)

    limites.indisponivel = False
    relogio.instante += DURACAO_DO_LEASE + timedelta(seconds=1)
    desfecho = caso_de_uso.executar(pedido(), escopo(), CTX)

    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA
    assert len(limites.aplicadas) == 1


def test_retry_depois_de_aplicado_no_limite_mas_nao_concluido_nao_reaplica(
    caso_de_uso: AutorizarTransacao,
    limites: LimitesFalso,
    relogio: RelogioAjustavel,
    repositorio: RepositorioDeAutorizacoesEmMemoria,
) -> None:
    """Commit incerto: o limite aplicou, o processo morreu antes de concluir. O retry obtém JA_APLICADA."""
    original_concluir = repositorio.concluir

    def morre_antes_de_concluir(*_: object) -> None:
        raise RuntimeError("processo morreu")

    repositorio.concluir = morre_antes_de_concluir  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        caso_de_uso.executar(pedido(), escopo(), CTX)
    repositorio.concluir = original_concluir  # type: ignore[method-assign]

    relogio.instante += DURACAO_DO_LEASE + timedelta(seconds=1)
    desfecho = caso_de_uso.executar(pedido(), escopo(), CTX)

    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA
    assert limites.disponivel == Decimal("850.00")  # reservado uma vez só


def test_estorno_de_reserva_libera_e_marca_a_original(
    caso_de_uso: AutorizarTransacao,
    limites: LimitesFalso,
    repositorio: RepositorioDeAutorizacoesEmMemoria,
) -> None:
    original = caso_de_uso.executar(pedido(), escopo("k-orig"), CTX).autorizacao

    estorno = caso_de_uso.executar(
        pedido(TipoOperacao.ESTORNO, id_autorizacao_original=original.id_autorizacao),
        escopo("k-estorno"),
        CTX,
    ).autorizacao

    assert estorno.status is StatusAutorizacao.APROVADA
    gravada = repositorio.obter(original.id_autorizacao)
    assert gravada is not None and gravada.status is StatusAutorizacao.ESTORNADA
    assert limites.disponivel == Decimal("1000.00")
    assert limites.chamadas[-1].tipo is TipoNoLimite.LIBERAR_RESERVA
    assert limites.chamadas[-1].id_operacao == id_operacao_de_estorno(original.id_autorizacao)
    assert isinstance(repositorio.outbox[-1], TransacaoEstornada)


def test_estorno_de_original_ja_estornada_e_conflito(caso_de_uso: AutorizarTransacao) -> None:
    original = caso_de_uso.executar(pedido(), escopo("k-orig"), CTX).autorizacao
    estorno = pedido(TipoOperacao.ESTORNO, id_autorizacao_original=original.id_autorizacao)
    caso_de_uso.executar(estorno, escopo("k-e1"), CTX)

    with pytest.raises(ConflitoDeEstado):
        caso_de_uso.executar(estorno, escopo("k-e2"), CTX)


@pytest.mark.parametrize(("valor", "original_id"), [("10.00", None), ("150.00", "nao-existe")])
def test_estorno_invalido(
    caso_de_uso: AutorizarTransacao, valor: str, original_id: str | None
) -> None:
    original = caso_de_uso.executar(pedido(), escopo("k-orig"), CTX).autorizacao

    with pytest.raises(AutorizacaoOriginalInvalida):
        caso_de_uso.executar(
            pedido(
                TipoOperacao.ESTORNO,
                valor=valor,
                id_autorizacao_original=original_id or original.id_autorizacao,
            ),
            escopo("k-estorno"),
            CTX,
        )


def test_dois_estornos_concorrentes_da_mesma_original_estornam_uma_vez(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    original = caso_de_uso.executar(
        pedido(TipoOperacao.DEBITO, valor="300.00"), escopo("k-orig"), CTX
    ).autorizacao
    estorno = pedido(
        TipoOperacao.ESTORNO, valor="300.00", id_autorizacao_original=original.id_autorizacao
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        desfechos = list(
            pool.map(
                lambda k: _executar_ou_conflito(caso_de_uso, estorno, k),
                [f"e{i}" for i in range(8)],
            )
        )

    aprovados = [d for d in desfechos if d == "APROVADA"]
    assert len(aprovados) == 1
    assert set(desfechos) <= {"APROVADA", "ORIGINAL_JA_ENCERRADA", "ESTORNO_EM_ANDAMENTO", "409"}
    assert limites.disponivel == Decimal("1000.00")  # devolveu 300 uma vez, não 600


def _executar_ou_conflito(
    caso_de_uso: AutorizarTransacao, estorno: PedidoDeAutorizacao, chave: str
) -> str:
    try:
        a = caso_de_uso.executar(estorno, escopo(chave), CTX).autorizacao
    except ConflitoDeEstado:
        return "409"  # a validação já viu a original ESTORNADA
    return str(a.motivo_recusa or a.status)


def test_estorno_com_outro_estorno_parado_nao_reaplica_o_id_derivado(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, relogio: RelogioAjustavel
) -> None:
    """O id da operação de estorno é derivado da original. Se o 1º estorno parou depois de aplicar e o 2º
    chegasse 6 min depois, a guarda do limite já teria esquecido o id: a marca na original impede."""
    original = caso_de_uso.executar(
        pedido(TipoOperacao.DEBITO, valor="300.00"), escopo("k-orig"), CTX
    ).autorizacao
    estorno = pedido(
        TipoOperacao.ESTORNO, valor="300.00", id_autorizacao_original=original.id_autorizacao
    )
    limites.indisponivel = True
    with pytest.raises(LimitesIndisponivel):
        caso_de_uso.executar(estorno, escopo("k-e1"), CTX)  # fica EM_PROCESSAMENTO com a marca
    limites.indisponivel = False
    relogio.instante = INICIO + timedelta(minutes=6)
    chamadas = len(limites.chamadas)

    segundo = caso_de_uso.executar(estorno, escopo("k-e2"), CTX).autorizacao

    assert segundo.motivo_recusa is MotivoRecusa.ESTORNO_EM_ANDAMENTO
    assert len(limites.chamadas) == chamadas


def test_marca_de_estorno_recusado_e_assumida_pelo_proximo(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    original = caso_de_uso.executar(
        pedido(TipoOperacao.DEBITO, valor="300.00"), escopo("k-orig"), CTX
    ).autorizacao
    estorno = pedido(
        TipoOperacao.ESTORNO, valor="300.00", id_autorizacao_original=original.id_autorizacao
    )
    aplicar = limites.aplicar

    def recusa_uma_vez(id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        limites.aplicar = aplicar  # type: ignore[method-assign]
        return ResultadoNoLimite(
            DesfechoNoLimite.RECUSADA, limites._saldos(), "CREDITO_EXCEDE_UTILIZADO"
        )

    limites.aplicar = recusa_uma_vez  # type: ignore[method-assign]
    primeiro = caso_de_uso.executar(estorno, escopo("k-e1"), CTX).autorizacao
    segundo = caso_de_uso.executar(estorno, escopo("k-e2"), CTX).autorizacao

    assert primeiro.motivo_recusa is MotivoRecusa.CREDITO_EXCEDE_UTILIZADO
    assert segundo.status is StatusAutorizacao.APROVADA
    assert limites.disponivel == Decimal("1000.00")


def _captura(reserva_id: str, valor: str) -> PedidoDeAutorizacao:
    return pedido(TipoOperacao.DEBITO, valor=valor, id_autorizacao_original=reserva_id)


def test_captura_parcial_liquida_o_hold_e_marca_a_reserva(
    caso_de_uso: AutorizarTransacao,
    limites: LimitesFalso,
    repositorio: RepositorioDeAutorizacoesEmMemoria,
) -> None:
    reserva = caso_de_uso.executar(pedido(valor="400.00"), escopo("k-res"), CTX).autorizacao

    captura = caso_de_uso.executar(
        _captura(reserva.id_autorizacao, "250.00"), escopo("k-cap"), CTX
    ).autorizacao

    assert captura.status is StatusAutorizacao.APROVADA
    gravada = repositorio.obter(reserva.id_autorizacao)
    assert gravada is not None and gravada.status is StatusAutorizacao.CAPTURADA
    assert limites.disponivel == Decimal("750.00")  # 250 consumidos, a sobra do hold voltou
    assert limites.chamadas[-1] == OperacaoNoLimite(
        TipoNoLimite.CAPTURAR_RESERVA,
        captura.id_autorizacao,
        Decimal("250.00"),
        reserva.id_autorizacao,
        valor_reservado=Decimal("400.00"),
        id_conta="0001-1",
    )
    autorizada, capturada = repositorio.outbox[-2:]
    assert isinstance(autorizada, TransacaoAutorizada)
    assert autorizada.id_autorizacao_original == reserva.id_autorizacao
    assert isinstance(capturada, ReservaCapturada)
    assert (capturada.valor_reservado, capturada.valor_capturado) == (
        Decimal("400.00"),
        Decimal("250.00"),
    )
    assert capturada.id_evento != autorizada.id_evento


def test_replay_da_captura_nao_captura_de_novo(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    reserva = caso_de_uso.executar(pedido(valor="400.00"), escopo("k-res"), CTX).autorizacao
    captura = _captura(reserva.id_autorizacao, "400.00")
    caso_de_uso.executar(captura, escopo("k-cap"), CTX)

    replay = caso_de_uso.executar(captura, escopo("k-cap"), CTX)

    assert replay.replay
    assert limites.disponivel == Decimal("600.00")


def test_captura_maior_que_a_reserva_ou_de_debito_e_invalida(
    caso_de_uso: AutorizarTransacao,
) -> None:
    reserva = caso_de_uso.executar(pedido(valor="400.00"), escopo("k-res"), CTX).autorizacao
    debito = caso_de_uso.executar(
        pedido(TipoOperacao.DEBITO, valor="10.00"), escopo("k-deb"), CTX
    ).autorizacao

    with pytest.raises(AutorizacaoOriginalInvalida):
        caso_de_uso.executar(_captura(reserva.id_autorizacao, "400.01"), escopo("k-1"), CTX)
    with pytest.raises(AutorizacaoOriginalInvalida):
        caso_de_uso.executar(_captura(debito.id_autorizacao, "10.00"), escopo("k-2"), CTX)


def test_reserva_capturada_nao_e_estornada_nem_capturada_de_novo(
    caso_de_uso: AutorizarTransacao,
) -> None:
    reserva = caso_de_uso.executar(pedido(valor="400.00"), escopo("k-res"), CTX).autorizacao
    caso_de_uso.executar(_captura(reserva.id_autorizacao, "400.00"), escopo("k-cap"), CTX)

    with pytest.raises(ConflitoDeEstado):
        caso_de_uso.executar(_captura(reserva.id_autorizacao, "400.00"), escopo("k-cap2"), CTX)
    with pytest.raises(ConflitoDeEstado):
        caso_de_uso.executar(
            pedido(
                TipoOperacao.ESTORNO, valor="400.00", id_autorizacao_original=reserva.id_autorizacao
            ),
            escopo("k-est"),
            CTX,
        )


def test_captura_que_perde_o_hold_e_recusada_e_nao_mexe_na_reserva(
    caso_de_uso: AutorizarTransacao,
    limites: LimitesFalso,
    repositorio: RepositorioDeAutorizacoesEmMemoria,
) -> None:
    reserva = caso_de_uso.executar(pedido(valor="400.00"), escopo("k-res"), CTX).autorizacao
    del limites.holds[reserva.id_autorizacao]  # estorno/expiração levou o hold depois da validação

    captura = caso_de_uso.executar(
        _captura(reserva.id_autorizacao, "400.00"), escopo("k-cap"), CTX
    ).autorizacao

    assert captura.motivo_recusa is MotivoRecusa.RESERVA_NAO_ENCONTRADA
    gravada = repositorio.obter(reserva.id_autorizacao)
    assert (
        gravada is not None and gravada.status is StatusAutorizacao.APROVADA
    )  # o vencedor encerra


# --- Prazo de reaplicação -------------------------------------------------------------------------


def test_retomada_fora_do_prazo_nao_reenvia_ao_limite(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, relogio: RelogioAjustavel
) -> None:
    limites.indisponivel = True
    with pytest.raises(LimitesIndisponivel):
        caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)
    limites.indisponivel = False
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO + timedelta(seconds=1)
    chamadas = len(limites.chamadas)

    with pytest.raises(EmReconciliacao):  # 409 terminal, sem Retry-After: repetir não muda nada
        caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)

    assert len(limites.chamadas) == chamadas  # a guarda do limite pode já ter esquecido o id


def test_retomada_dentro_do_prazo_reenvia_e_conclui(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, relogio: RelogioAjustavel
) -> None:
    limites.indisponivel = True
    with pytest.raises(LimitesIndisponivel):
        caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)
    limites.indisponivel = False
    relogio.instante = INICIO + PRAZO_DE_REAPLICACAO - timedelta(seconds=1)

    desfecho = caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)

    assert desfecho.autorizacao.status is StatusAutorizacao.APROVADA


# --- Painel 4 e banco de perguntas: caminho quente, BOLA, motivo desconhecido ----------------------


class RepositorioQueContaLeituras(RepositorioDeAutorizacoesEmMemoria):
    def __init__(self) -> None:
        super().__init__()
        self.leituras = 0

    def obter(self, id_autorizacao: str):  # type: ignore[no-untyped-def]
        self.leituras += 1
        return super().obter(id_autorizacao)


def test_caminho_quente_sem_original_nao_le_antes_de_gravar(
    limites: LimitesFalso, relogio: RelogioAjustavel
) -> None:
    """A economia de um GetItem por autorização fica protegida: o PutItem condicional é a consulta."""
    repo = RepositorioQueContaLeituras()
    caso = AutorizarTransacao(repo, limites, relogio, IdsSequenciais())

    caso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)
    replay = caso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)

    assert repo.leituras == 0
    assert replay.replay


def test_estorno_de_autorizacao_de_outro_canal_e_tratado_como_inexistente(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    debito = caso_de_uso.executar(
        pedido(TipoOperacao.DEBITO, "300.00"), escopo("d"), CTX
    ).autorizacao
    escopo_de_outro_canal = EscopoDeIdempotencia(
        "canal-intruso", "POST", "/v1/contratos/CTR-1/autorizacoes", "e"
    )

    with pytest.raises(AutorizacaoOriginalInvalida):
        caso_de_uso.executar(
            pedido(TipoOperacao.ESTORNO, "300.00", id_autorizacao_original=debito.id_autorizacao),
            escopo_de_outro_canal,
            CTX,
        )
    assert limites.disponivel == Decimal("700.00")


def test_motivo_novo_do_limite_vira_recusa_generica_e_nao_500(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    def recusa_com_motivo_novo(id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        return ResultadoNoLimite(DesfechoNoLimite.RECUSADA, limites._saldos(), "MOTIVO_DO_FUTURO")

    limites.aplicar = recusa_com_motivo_novo  # type: ignore[method-assign]

    recusada = caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX).autorizacao

    assert recusada.motivo_recusa is MotivoRecusa.RECUSA_DO_LIMITE


def test_motivo_desconhecido_vira_linha_json_para_o_alarme(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, caplog: pytest.LogCaptureFixture
) -> None:
    def recusa_com_motivo_novo(id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        return ResultadoNoLimite(DesfechoNoLimite.RECUSADA, limites._saldos(), "MOTIVO_DO_FUTURO")

    limites.aplicar = recusa_com_motivo_novo  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="autorizacao.application.autorizar"):
        caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k"), CTX)

    (linha,) = [r.getMessage() for r in caplog.records if "motivo_desconhecido" in r.getMessage()]
    assert json.loads(linha) == {
        "evento": "motivo_desconhecido_do_limite",
        "motivo": "MOTIVO_DO_FUTURO",
    }


def test_conta_divergente_e_mapeada_e_a_recusa_nao_guarda_saldos(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso, caplog: pytest.LogCaptureFixture
) -> None:
    def conta_errada(id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        return ResultadoNoLimite(DesfechoNoLimite.RECUSADA, limites._saldos(), "CONTA_DIVERGENTE")

    limites.aplicar = conta_errada  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="autorizacao.application.autorizar"):
        recusada = caso_de_uso.executar(pedido(), escopo("k"), CTX).autorizacao

    assert recusada.motivo_recusa is MotivoRecusa.CONTA_DIVERGENTE  # não o 422 genérico
    assert recusada.saldos is None  # o canal não vê o saldo de um contrato que não é dele
    assert "motivo_desconhecido" not in caplog.text


def test_operacoes_do_canal_levam_o_id_conta(
    caso_de_uso: AutorizarTransacao, limites: LimitesFalso
) -> None:
    reserva = caso_de_uso.executar(pedido(), escopo("k-r"), CTX).autorizacao
    caso_de_uso.executar(pedido(TipoOperacao.DEBITO), escopo("k-d"), CTX)
    caso_de_uso.executar(pedido(TipoOperacao.CREDITO), escopo("k-c"), CTX)
    caso_de_uso.executar(
        pedido(TipoOperacao.ESTORNO, id_autorizacao_original=reserva.id_autorizacao),
        escopo("k-e"),
        CTX,
    )

    assert [op.id_conta for op in limites.chamadas] == ["0001-1"] * 4
