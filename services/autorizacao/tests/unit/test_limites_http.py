"""GatewayDeLimitesHttp contra httpx.MockTransport (nenhuma rede, nenhum boto3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from autorizacao.adapters.outbound.disjuntor import Disjuntor, EstadoDoDisjuntor
from autorizacao.adapters.outbound.limites_http import GatewayDeLimitesHttp
from autorizacao.application.portas import (
    ContratoInexistente,
    DesfechoNoLimite,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    TipoNoLimite,
)
from plataforma.observabilidade import definir_contexto, limpar_contexto

ID_OP = "6f1c2b0e-3d4a-5b6c-8d7e-9f0a1b2c3d4e"
TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
RESPOSTA_OK = (
    '{"resultado": "APLICADA", "saldos": {"limite_concedido": 1000.00, "limite_utilizado": 0.00, '
    '"saldo_reservado": 150.10, "limite_disponivel": 849.90, "versao": 7}}'
)


def _gateway(
    tratador: Callable[[httpx.Request], httpx.Response], disjuntor: Disjuntor | None = None
) -> tuple[GatewayDeLimitesHttp, Disjuntor, list[float]]:
    disjuntor = disjuntor or Disjuntor()
    dormidas: list[float] = []
    cliente = httpx.Client(base_url="http://limites", transport=httpx.MockTransport(tratador))
    gateway = GatewayDeLimitesHttp(
        cliente, disjuntor, dormir=dormidas.append, aleatorio=lambda: 0.5
    )
    return gateway, disjuntor, dormidas


def _op(valor: str = "150.10", **extra: str) -> OperacaoNoLimite:
    return OperacaoNoLimite(TipoNoLimite.RESERVAR, ID_OP, Decimal(valor), **extra)  # type: ignore[arg-type]


def test_200_aplicada_traduz_saldos_com_decimal_exato() -> None:
    gateway, disjuntor, _ = _gateway(lambda _: httpx.Response(200, text=RESPOSTA_OK))

    resultado = gateway.aplicar("CTR-1", _op())

    assert resultado.desfecho is DesfechoNoLimite.APLICADA
    assert resultado.saldos.saldo_reservado == Decimal("150.10")
    assert str(resultado.saldos.limite_disponivel) == "849.90"
    assert resultado.saldos.versao == 7
    assert resultado.motivo is None
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


def test_200_recusada_carrega_o_motivo() -> None:
    texto = RESPOSTA_OK.replace('"APLICADA"', '"RECUSADA", "motivo": "LIMITE_INSUFICIENTE"')
    gateway, _, _ = _gateway(lambda _: httpx.Response(200, text=texto))

    resultado = gateway.aplicar("CTR-1", _op())

    assert resultado.desfecho is DesfechoNoLimite.RECUSADA
    assert resultado.motivo == "LIMITE_INSUFICIENTE"


def test_404_vira_contrato_inexistente_e_conta_como_sucesso_do_servico() -> None:
    disjuntor = Disjuntor()
    for _ in range(4):
        disjuntor.registrar_falha()
    gateway, _, _ = _gateway(lambda _: httpx.Response(404, json={"codigo": "X"}), disjuntor)

    with pytest.raises(ContratoInexistente):
        gateway.aplicar("CTR-X", _op())

    for _ in range(4):  # a contagem de falhas foi zerada pelo 404
        disjuntor.registrar_falha()
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


def test_503_nas_duas_tentativas_vira_indisponivel_e_o_disjuntor_contabiliza() -> None:
    chamadas: list[httpx.Request] = []

    def tratador(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        return httpx.Response(503)

    gateway, disjuntor, dormidas = _gateway(tratador)

    with pytest.raises(LimitesIndisponivel):
        gateway.aplicar("CTR-1", _op())

    assert len(chamadas) == 2
    assert dormidas == [0.5 * 0.05]  # full jitter: aleatorio() * min(0.2, 0.05 * 2**0)
    for _ in range(3):  # 2 já contadas + 3 = 5 -> abre
        disjuntor.registrar_falha()
    assert disjuntor.estado is EstadoDoDisjuntor.ABERTO


def test_timeout_faz_retry_e_a_segunda_tentativa_pode_dar_certo() -> None:
    tentativas: list[int] = []

    def tratador(req: httpx.Request) -> httpx.Response:
        tentativas.append(1)
        if len(tentativas) == 1:
            raise httpx.ReadTimeout("lento", request=req)
        return httpx.Response(200, text=RESPOSTA_OK)

    gateway, _, dormidas = _gateway(tratador)

    resultado = gateway.aplicar("CTR-1", _op())

    assert resultado.desfecho is DesfechoNoLimite.APLICADA
    assert len(tentativas) == 2
    assert len(dormidas) == 1


def test_erro_de_transporte_tambem_e_retentado() -> None:
    def tratador(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusou", request=req)

    gateway, _, _ = _gateway(tratador)

    with pytest.raises(LimitesIndisponivel):
        gateway.aplicar("CTR-1", _op())


def test_corpo_tem_numero_exato_e_headers_de_correlacao() -> None:
    capturada: list[httpx.Request] = []

    def tratador(req: httpx.Request) -> httpx.Response:
        capturada.append(req)
        return httpx.Response(200, text=RESPOSTA_OK)

    gateway, _, _ = _gateway(tratador)
    tokens = definir_contexto("corr-123", TP)
    try:
        gateway.aplicar("CTR-1", _op("12345678901234.56", id_reserva=ID_OP))
    finally:
        limpar_contexto(tokens)

    (req,) = capturada
    assert req.method == "POST"
    assert req.url.path == "/interno/v1/contratos/CTR-1/operacoes"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-correlation-id"] == "corr-123"
    assert req.headers["traceparent"] == TP
    texto = req.content.decode()
    assert '"valor": 12345678901234.56' in texto  # número cru, sem float
    assert json.loads(texto)["id_operacao"] == ID_OP
    assert json.loads(texto)["id_reserva"] == ID_OP
    assert json.loads(texto)["tipo"] == "RESERVAR"


def test_sem_contexto_nao_envia_headers_de_correlacao() -> None:
    capturada: list[httpx.Request] = []

    def tratador(req: httpx.Request) -> httpx.Response:
        capturada.append(req)
        return httpx.Response(200, text=RESPOSTA_OK)

    gateway, _, _ = _gateway(tratador)
    gateway.aplicar("CTR-1", _op())

    assert "x-correlation-id" not in capturada[0].headers
    assert "traceparent" not in capturada[0].headers


def test_disjuntor_aberto_nao_chama_a_rede() -> None:
    chamadas: list[httpx.Request] = []

    def tratador(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        return httpx.Response(200, text=RESPOSTA_OK)

    disjuntor = Disjuntor()
    for _ in range(5):
        disjuntor.registrar_falha()
    gateway, _, _ = _gateway(tratador, disjuntor)

    with pytest.raises(LimitesIndisponivel):
        gateway.aplicar("CTR-1", _op())

    assert chamadas == []


def test_422_e_bug_de_contrato_e_nao_conta_como_falha() -> None:
    gateway, disjuntor, _ = _gateway(lambda _: httpx.Response(422, json={}))

    with pytest.raises(RuntimeError):
        gateway.aplicar("CTR-1", _op())

    for _ in range(4):
        disjuntor.registrar_falha()
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


def test_prova_do_meio_aberto_com_4xx_inesperado_nao_prende_o_disjuntor() -> None:
    """Regressão: 4xx na chamada de prova deixava o disjuntor em 'prova em curso' para sempre."""
    instante = [0.0]
    disjuntor = Disjuntor(limiar_falhas=1, pausa_s=5.0, relogio=lambda: instante[0])
    disjuntor.registrar_falha()  # abre
    instante[0] = 6.0  # pausa vencida → meio-aberto
    cliente = httpx.Client(
        base_url="http://limites", transport=httpx.MockTransport(lambda _: httpx.Response(422))
    )
    gateway = GatewayDeLimitesHttp(cliente, disjuntor, dormir=lambda _: None)

    with pytest.raises(RuntimeError):
        gateway.aplicar("CTR-1", OperacaoNoLimite(TipoNoLimite.DEBITAR, ID_OP, Decimal("1.00")))

    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO
    assert disjuntor.permitir()


def test_cada_tentativa_de_aplicar_leva_um_prazo_proprio() -> None:
    prazos: list[int] = []
    instantes = iter([1000.0, 1003.0])

    def tratador(req: httpx.Request) -> httpx.Response:
        prazos.append(int(req.headers["x-prazo-ms"]))
        if len(prazos) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "resultado": "APLICADA",
                "saldos": {
                    "limite_concedido": 1, "limite_utilizado": 0, "saldo_reservado": 1,
                    "limite_disponivel": 0, "versao": 2,
                },
            },
        )  # fmt: skip

    cliente = httpx.Client(base_url="http://limites", transport=httpx.MockTransport(tratador))
    gateway = GatewayDeLimitesHttp(
        cliente, Disjuntor(), dormir=lambda _: None, relogio=lambda: next(instantes)
    )

    gateway.aplicar("CTR-1", _op())

    assert prazos == [1_002_000, 1_005_000]  # agora + 2 s, recalculado na retentativa


def test_consulta_da_operacao_nao_leva_prazo_e_interpreta_a_resposta() -> None:
    def tratador(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and "x-prazo-ms" not in req.headers
        assert req.url.path == f"/interno/v1/contratos/CTR-1/operacoes/{ID_OP}"
        return httpx.Response(
            200, json={"id_operacao": ID_OP, "na_guarda": False, "reserva_ativa": True, "versao": 3}
        )

    gateway, _, _ = _gateway(tratador)

    assert gateway.consultar_operacao("CTR-1", ID_OP) == EstadoNoLimite(False, True)


def test_consulta_de_contrato_inexistente_vira_contrato_inexistente() -> None:
    gateway, _, _ = _gateway(
        lambda _: httpx.Response(404, json={"codigo": "CONTRATO_NAO_ENCONTRADO"})
    )

    with pytest.raises(ContratoInexistente):
        gateway.consultar_operacao("CTR-X", ID_OP)
