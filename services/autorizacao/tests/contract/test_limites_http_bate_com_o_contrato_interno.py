"""Fronteira autorizacao → limites do lado CONSUMIDOR (C7-4, painel 7): o `GatewayDeLimitesHttp` envia o que
contracts/openapi/limites-interno-v1.yaml declara e interpreta exatamente o que ele declara (status, campos e
motivos). O lado fornecedor é `services/limites/tests/contract/test_api_interna_bate_com_o_contrato.py`.

As requisições saem dos caminhos reais que falam com o limite (tradução do pedido do canal, expirador e
compensação do reconciliador) e passam por um `httpx.MockTransport` que confere cada uma contra o contrato: path,
método, headers próprios declarados, corpo no schema `Operacao` e as obrigatoriedades que o contrato escreve em
prosa. As respostas usadas nos testes de interpretação são validadas antes contra o schema declarado para o status.
Sem rede.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from autorizacao.adapters.outbound.disjuntor import Disjuntor, EstadoDoDisjuntor
from autorizacao.adapters.outbound.limites_http import GatewayDeLimitesHttp
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao, _operacao_no_limite
from autorizacao.application.expirar import ExpirarReservas, ResultadoDaExpiracao
from autorizacao.application.portas import (
    ContratoInexistente,
    DesfechoNoLimite,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    TipoNoLimite,
)
from autorizacao.application.reconciliar import GRACA, ReconciliarOrfas, ResultadoDaReconciliacao
from autorizacao.domain.autorizacao import (
    DURACAO_DO_LEASE,
    VALIDADE_DA_RESERVA,
    Autorizacao,
    MotivoRecusa,
)
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao
from jsonschema import Draft4Validator, FormatChecker
from plataforma.observabilidade import definir_contexto, limpar_contexto

RAIZ = Path(__file__).resolve().parents[4]
CONTRATO: dict[str, Any] = yaml.safe_load(
    (RAIZ / "contracts/openapi/limites-interno-v1.yaml").read_text(encoding="utf-8")
)
APLICAR = ("/interno/v1/contratos/{id_contrato}/operacoes", "post")
CONSULTAR = ("/interno/v1/contratos/{id_contrato}/operacoes/{id_operacao}", "get")
INICIO = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
CTR = "CTR-1"
TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
# Headers que o próprio HTTP/httpx põe em toda requisição; os demais o consumidor escolheu mandar.
HEADERS_DO_TRANSPORTE = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "content-length",
    "content-type",
}


def _json_schema(no: Any) -> Any:
    """OpenAPI 3.0 → JSON Schema Draft 4: o único desvio usado no contrato é `nullable`."""
    if isinstance(no, list):
        return [_json_schema(item) for item in no]
    if not isinstance(no, dict):
        return no
    convertido = {chave: _json_schema(valor) for chave, valor in no.items() if chave != "nullable"}
    if no.get("nullable") is True and "type" in no:
        convertido["type"] = [no["type"], "null"]
    return convertido


COMPONENTES = _json_schema(CONTRATO["components"])


def _no(no: Any) -> Any:
    if isinstance(no, dict) and "$ref" in no:
        alvo: Any = CONTRATO
        for parte in no["$ref"].removeprefix("#/").split("/"):
            alvo = alvo[parte]
        return alvo
    return no


def _erros(schema: Any, instancia: Any) -> list[str]:
    validador = Draft4Validator(
        {**_json_schema(schema), "components": COMPONENTES}, format_checker=FormatChecker()
    )
    return [f"{list(e.absolute_path)}: {e.message}" for e in validador.iter_errors(instancia)]


def _operacao(rota: tuple[str, str]) -> dict[str, Any]:
    caminho, metodo = rota
    return dict(CONTRATO["paths"][caminho][metodo])


def _respostas(rota: tuple[str, str]) -> dict[str, Any]:
    return {status: _no(r) for status, r in _operacao(rota)["responses"].items()}


# --- O lado do limites, conferindo o que o consumidor manda -------------------------------------------


def _rota_da(requisicao: httpx.Request) -> tuple[tuple[str, str], dict[str, str]] | None:
    for caminho, operacoes in CONTRATO["paths"].items():
        molde = re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", caminho)
        casou = re.fullmatch(molde, requisicao.url.path)
        if casou and requisicao.method.lower() in operacoes:
            return (caminho, requisicao.method.lower()), casou.groupdict()
    return None


def problemas_na_requisicao(requisicao: httpx.Request) -> list[str]:
    """Tudo o que nesta requisição o contrato interno não declara (lista vazia = conforme)."""
    achada = _rota_da(requisicao)
    if achada is None:
        return [f"{requisicao.method} {requisicao.url.path}: rota fora do contrato"]
    rota, valores = achada
    problemas: list[str] = []
    declarados = {p["name"].lower(): p for p in map(_no, _operacao(rota).get("parameters", []))}
    for nome, valor in valores.items():
        problemas += [
            f"path {nome}: {e}" for e in _erros(declarados[nome.lower()]["schema"], valor)
        ]
    for nome, valor in requisicao.headers.items():
        if nome.lower() in HEADERS_DO_TRANSPORTE:
            continue
        parametro = declarados.get(nome.lower())
        if parametro is None or parametro["in"] != "header":
            problemas.append(f"header {nome} não declarado")
            continue
        tipo = parametro["schema"].get("type")
        instancia = int(valor) if tipo == "integer" and valor.isdigit() else valor
        problemas += [f"header {nome}: {e}" for e in _erros(parametro["schema"], instancia)]
    if "requestBody" in _operacao(rota):
        corpo = json.loads(requisicao.content)
        schema = _operacao(rota)["requestBody"]["content"]["application/json"]["schema"]
        problemas += [f"corpo: {e}" for e in _erros(schema, corpo)]
        # Obrigatoriedades que o contrato escreve em prosa (descrições de `id_reserva` e `valor_reservado`).
        if (
            corpo.get("tipo") in ("LIBERAR_RESERVA", "CAPTURAR_RESERVA")
            and "id_reserva" not in corpo
        ):
            problemas.append(f"corpo: {corpo['tipo']} sem id_reserva")
        if corpo.get("tipo") == "CAPTURAR_RESERVA" and "valor_reservado" not in corpo:
            problemas.append("corpo: CAPTURAR_RESERVA sem valor_reservado")
    return problemas


SALDOS = {
    "limite_concedido": 1000.00,
    "limite_utilizado": 100.00,
    "saldo_reservado": 150.00,
    "limite_disponivel": 750.00,
    "versao": 3,
}


@dataclass
class LimitesDoContrato:
    """Faz o papel do `limites`: confere cada requisição e responde APLICADA (ou "aplicada e com hold ativo", na
    consulta), com corpos válidos no schema declarado."""

    recebidas: list[httpx.Request] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)

    def __call__(self, requisicao: httpx.Request) -> httpx.Response:
        self.recebidas.append(requisicao)
        self.problemas += problemas_na_requisicao(requisicao)
        if requisicao.method == "GET":
            id_operacao = requisicao.url.path.rsplit("/", 1)[-1]
            estado = {"id_operacao": id_operacao, "na_guarda": True, "reserva_ativa": True}
            return httpx.Response(200, json={**estado, "versao": 3})
        return httpx.Response(200, json={"resultado": "APLICADA", "saldos": SALDOS})

    def corpos(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.recebidas if r.method == "POST"]


def _gateway(
    tratador: Callable[[httpx.Request], httpx.Response], disjuntor: Disjuntor | None = None
) -> GatewayDeLimitesHttp:
    cliente = httpx.Client(base_url="http://limites", transport=httpx.MockTransport(tratador))
    return GatewayDeLimitesHttp(cliente, disjuntor or Disjuntor(), dormir=lambda _: None)


@dataclass
class Relogio:
    instante: datetime = INICIO

    def agora(self) -> datetime:
        return self.instante


@dataclass
class Ids:
    _c: count[int] = field(default_factory=count)

    def novo_id(self) -> str:
        return str(uuid.UUID(int=next(self._c) + 1))


def _autorizacao(
    tipo: TipoOperacao, original: str | None = None, agora: datetime = INICIO
) -> Autorizacao:
    return Autorizacao.iniciar(
        PedidoDeAutorizacao(
            CTR, "0001-1", Decimal("150.00"), "BRL", tipo, id_autorizacao_original=original
        ),
        id_autorizacao=str(uuid.uuid4()),
        impressao_digital="imp",
        client_id="canal-pos",
        agora=agora,
        correlation_id=str(uuid.uuid4()),
        traceparent=None,
    )


SALDOS_DO_DOMINIO = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 2
)


def _operacoes_do_canal() -> list[OperacaoNoLimite]:
    """A tradução real do pedido do canal (`_operacao_no_limite`) para cada tipo, captura e os dois estornos."""
    reserva = _autorizacao(TipoOperacao.RESERVA).aprovar(SALDOS_DO_DOMINIO, INICIO, "ev")
    debito = _autorizacao(TipoOperacao.DEBITO).aprovar(SALDOS_DO_DOMINIO, INICIO, "ev")
    casos = [
        (_autorizacao(TipoOperacao.RESERVA), None),
        (_autorizacao(TipoOperacao.DEBITO), None),
        (_autorizacao(TipoOperacao.CREDITO), None),
        (_autorizacao(TipoOperacao.DEBITO, reserva.id_autorizacao), reserva),
        (_autorizacao(TipoOperacao.ESTORNO, reserva.id_autorizacao), reserva),
        (_autorizacao(TipoOperacao.ESTORNO, debito.id_autorizacao), debito),
    ]
    return [_operacao_no_limite(a, original) for a, original in casos]


# --- O que o consumidor manda ---------------------------------------------------------------------------


def test_operacoes_pedidas_pelo_canal_saem_como_o_contrato_declara() -> None:
    limites = LimitesDoContrato()
    gateway = _gateway(limites)
    tokens = definir_contexto(
        str(uuid.uuid4()), TP, None
    )  # correlação e trace propagados no header
    try:
        for operacao in _operacoes_do_canal():
            gateway.aplicar(CTR, operacao)
    finally:
        limpar_contexto(tokens)

    assert limites.problemas == []
    assert {c["tipo"] for c in limites.corpos()} == set(TipoNoLimite)  # os seis tipos do contrato
    assert all("id_conta" in c for c in limites.corpos())  # pedido do canal leva a conta (BOLA)
    assert all(
        {"x-correlation-id", "traceparent", "x-prazo-ms"} <= set(r.headers)
        for r in limites.recebidas
    )


def test_expiracao_e_compensacao_do_reconciliador_saem_como_o_contrato_declara() -> None:
    """Os caminhos automáticos (sem `id_conta`): liberação por expiração, consulta só de leitura e reversões."""
    limites = LimitesDoContrato()
    gateway = _gateway(limites)
    repo, relogio, ids = RepositorioDeAutorizacoesEmMemoria(), Relogio(), Ids()
    reserva = _autorizacao(TipoOperacao.RESERVA)
    repo.registrar(reserva)
    repo.concluir(reserva.aprovar(SALDOS_DO_DOMINIO, INICIO, ids.novo_id()), None)
    for tipo in (
        TipoOperacao.RESERVA,
        TipoOperacao.DEBITO,
    ):  # órfãs que o reconciliador vai desfazer
        repo.registrar(_autorizacao(tipo))
    autorizar = AutorizarTransacao(repo, gateway, relogio, ids)

    relogio.instante = INICIO + DURACAO_DO_LEASE + GRACA + timedelta(seconds=1)
    compensadas = ReconciliarOrfas(repo, gateway, relogio, autorizar).executar()
    relogio.instante = INICIO + VALIDADE_DA_RESERVA + timedelta(seconds=1)
    expiradas = ExpirarReservas(repo, gateway, relogio, ids).executar()

    assert compensadas == {ResultadoDaReconciliacao.COMPENSADA: 2}
    assert expiradas == {ResultadoDaExpiracao.EXPIRADA: 1}
    assert limites.problemas == []
    assert sorted(c["tipo"] for c in limites.corpos()) == [
        "ESTORNAR_DEBITO",
        "LIBERAR_RESERVA",
        "LIBERAR_RESERVA",
    ]
    assert not any("id_conta" in c for c in limites.corpos())
    assert sum(r.method == "GET" for r in limites.recebidas) == 2


def test_o_conferidor_reprova_requisicao_fora_do_contrato() -> None:
    """Sanidade: tipo desconhecido, campo a mais, captura sem valor_reservado e header não declarado."""
    corpo = {"id_operacao": str(uuid.uuid4()), "tipo": "CAPTURAR_RESERVA", "valor": 1, "x": 1}
    requisicao = httpx.Request(
        "POST",
        "http://limites/interno/v1/contratos/CTR-1/operacoes",
        json=corpo,
        headers={"X-Inventado": "1"},
    )

    problemas = " | ".join(problemas_na_requisicao(requisicao))

    assert "valor_reservado" in problemas and "id_reserva" in problemas
    assert "Additional properties" in problemas and "x-inventado" in problemas.lower()


# --- O que o consumidor entende ------------------------------------------------------------------------


def _resposta_valida(rota: tuple[str, str], status: int, corpo: dict[str, Any]) -> httpx.Response:
    """Só responde com o que o contrato declara para o status: corpo válido no schema daquela resposta."""
    declarada = _respostas(rota)[str(status)]
    ((media_type, conteudo),) = declarada["content"].items()
    assert _erros(conteudo["schema"], corpo) == [], (status, corpo)
    return httpx.Response(
        status, content=json.dumps(corpo).encode(), headers={"content-type": media_type}
    )


def _problema(status: int) -> dict[str, Any]:
    return {
        "type": "https://financas.example/problemas/x",
        "title": "x",
        "status": status,
        "codigo": "X",
    }


RESULTADOS: list[str] = COMPONENTES["schemas"]["ResultadoDaOperacao"]["properties"]["resultado"][
    "enum"
]
MOTIVOS: list[str] = COMPONENTES["schemas"]["ResultadoDaOperacao"]["properties"]["motivo"]["enum"]
RESERVA = OperacaoNoLimite(TipoNoLimite.RESERVAR, str(uuid.uuid4()), Decimal("150.00"))


@pytest.mark.parametrize(
    ("resultado", "motivo"),
    [(r, None) for r in RESULTADOS if r != "RECUSADA"] + [("RECUSADA", m) for m in MOTIVOS],
)
def test_todo_resultado_e_motivo_declarados_sao_entendidos(
    resultado: str, motivo: str | None
) -> None:
    corpo: dict[str, Any] = {"resultado": resultado, "saldos": SALDOS}
    if motivo is not None:
        corpo["motivo"] = motivo
    gateway = _gateway(lambda _: _resposta_valida(APLICAR, 200, corpo))

    lido = gateway.aplicar(CTR, RESERVA)

    assert (lido.desfecho, lido.motivo) == (DesfechoNoLimite(resultado), motivo)
    assert lido.saldos.versao == 3


def test_todo_motivo_declarado_pelo_fornecedor_tem_nome_no_dominio() -> None:
    """Nenhum motivo do contrato cai na recusa genérica RECUSA_DO_LIMITE: o domínio conhece todos."""
    assert set(MOTIVOS) <= {m.value for m in MotivoRecusa}


def test_saldos_saem_exatos_e_campo_e_motivo_novos_sao_tolerados() -> None:
    """O contrato manda tolerar valor novo de `motivo` e não fecha `ResultadoDaOperacao` a campos novos."""
    texto = (
        '{"resultado": "RECUSADA", "motivo": "MOTIVO_DO_FUTURO", "campo_novo": 1, "saldos": '
        '{"limite_concedido": 9999999999999999.99, "limite_utilizado": 0.01, "saldo_reservado": 0, '
        '"limite_disponivel": 9999999999999999.98, "versao": 9}}'
    )
    gateway = _gateway(lambda _: httpx.Response(200, text=texto))

    lido = gateway.aplicar(CTR, RESERVA)

    assert (lido.desfecho, lido.motivo) == (DesfechoNoLimite.RECUSADA, "MOTIVO_DO_FUTURO")
    assert lido.saldos.limite_concedido == Decimal("9999999999999999.99")
    assert lido.saldos.limite_disponivel == Decimal("9999999999999999.98")


# Cada status declarado tem uma interpretação escolhida: 404 é estado (contrato inexistente), 400/422 são bug do
# consumidor (erro, sem abrir o disjuntor: o limites respondeu) e 5xx é indisponibilidade (retry, depois 503).
INTERPRETACAO_DO_APLICAR: dict[int, type[Exception] | None] = {
    200: None,
    400: RuntimeError,
    404: ContratoInexistente,
    422: RuntimeError,
    500: LimitesIndisponivel,
    503: LimitesIndisponivel,
}
INTERPRETACAO_DA_CONSULTA: dict[int, type[Exception] | None] = {
    200: None,
    404: ContratoInexistente,
    422: RuntimeError,
    500: LimitesIndisponivel,
    503: LimitesIndisponivel,
}


def test_todo_status_declarado_tem_interpretacao_no_consumidor() -> None:
    assert set(map(str, INTERPRETACAO_DO_APLICAR)) == set(_respostas(APLICAR))
    assert set(map(str, INTERPRETACAO_DA_CONSULTA)) == set(_respostas(CONSULTAR))


@pytest.mark.parametrize(("status", "esperada"), INTERPRETACAO_DO_APLICAR.items())
def test_aplicar_interpreta_cada_status_declarado(
    status: int, esperada: type[Exception] | None
) -> None:
    corpo = {"resultado": "APLICADA", "saldos": SALDOS} if status == 200 else _problema(status)
    chamadas: list[int] = []

    def responder(_: httpx.Request) -> httpx.Response:
        chamadas.append(status)
        return _resposta_valida(APLICAR, status, corpo)

    disjuntor = Disjuntor()
    gateway = _gateway(responder, disjuntor)

    if esperada is None:
        assert gateway.aplicar(CTR, RESERVA).desfecho is DesfechoNoLimite.APLICADA
    else:
        with pytest.raises(esperada):
            gateway.aplicar(CTR, RESERVA)
    assert len(chamadas) == (2 if status >= 500 else 1)  # só 5xx é repetido
    assert disjuntor.estado is EstadoDoDisjuntor.FECHADO


@pytest.mark.parametrize(("status", "esperada"), INTERPRETACAO_DA_CONSULTA.items())
def test_consulta_interpreta_cada_status_declarado(
    status: int, esperada: type[Exception] | None
) -> None:
    id_operacao = str(uuid.uuid4())
    corpo = (
        {"id_operacao": id_operacao, "na_guarda": True, "reserva_ativa": False, "versao": 2}
        if status == 200
        else _problema(status)
    )
    gateway = _gateway(lambda _: _resposta_valida(CONSULTAR, status, corpo))

    if esperada is None:
        assert gateway.consultar_operacao(CTR, id_operacao) == EstadoNoLimite(True, False)
    else:
        with pytest.raises(esperada):
            gateway.consultar_operacao(CTR, id_operacao)
