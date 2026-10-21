"""Testes do adapter HTTP público sobre `criar_app` (repositório em memória, Limites falso)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import jsonschema
import pytest
from autorizacao.adapters.inbound.http import (
    _CODIGO_DA_RECUSA,
    _DETALHE_DA_RECUSA,
    _DETALHE_DO_MOTIVO,
    _PROBLEMAS,
    MAX_ERROS_POR_PROBLEMA,
    criar_app,
)
from autorizacao.adapters.outbound.cloudevents import para_cloudevent, serializar
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.consultar import ConsultarAutorizacao
from autorizacao.application.portas import (
    ArmazenamentoIndisponivel,
    ContratoInexistente,
    DesfechoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.domain.autorizacao import MotivoRecusa
from autorizacao.domain.eventos import SaldosDoLimite, TransacaoAutorizada, TransacaoRecusada
from fastapi.testclient import TestClient
from plataforma.observabilidade import gateway_request_id_atual

CTR = "CTR-1"
URL = f"/v1/contratos/{CTR}/autorizacoes"
PROBLEM_JSON = "application/problem+json"
AGORA = datetime(2026, 10, 20, 13, 0, 0, 123456, tzinfo=UTC)


class RelogioFixo:
    def agora(self) -> datetime:
        return AGORA


@dataclass
class IdsSequenciais:
    _contador: count[int] = field(default_factory=count)

    def novo_id(self) -> str:
        """UUID determinístico: o id do evento vira o `id` do CloudEvent, que o schema exige UUID."""
        return str(uuid.UUID(int=next(self._contador) + 1))


@dataclass
class LimitesFalso:
    """Dublê do port com as MESMAS pré-condições do agregado `Limite` real (LSP), não mais permissivo que ele:
    idempotência antes de qualquer recusa; conta divergente da titular → CONTA_DIVERGENTE; RESERVAR/DEBITAR
    exigem disponível; CREDITAR/ESTORNAR_DEBITO exigem utilizado ≥ valor; LIBERAR/CAPTURAR exigem o hold com o
    valor gravado. Saldos com as invariantes do real: disponível = concedido − utilizado − reservado, e
    utilizado/reservado nunca negativos."""

    concedido: Decimal = Decimal("1000.00")
    contas: dict[str, str] = field(default_factory=lambda: {CTR: "0001-1"})  # contrato → titular
    indisponivel: bool = False
    utilizado: Decimal = Decimal("0.00")
    holds: dict[str, Decimal] = field(default_factory=dict)
    aplicadas: dict[str, OperacaoNoLimite] = field(default_factory=dict)
    _versao: int = 1

    @property
    def reservado(self) -> Decimal:
        return sum(self.holds.values(), Decimal("0.00"))

    @property
    def disponivel(self) -> Decimal:
        return self.concedido - self.utilizado - self.reservado

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        if self.indisponivel:
            raise LimitesIndisponivel("circuit breaker aberto")
        if id_contrato not in self.contas:
            raise ContratoInexistente(id_contrato)
        if operacao.id_operacao in self.aplicadas:
            return ResultadoNoLimite(DesfechoNoLimite.JA_APLICADA, self._saldos())
        motivo = self._recusa(id_contrato, operacao)
        if motivo is not None:
            return ResultadoNoLimite(DesfechoNoLimite.RECUSADA, self._saldos(), motivo)
        match operacao.tipo:
            case TipoNoLimite.RESERVAR:
                self.holds[operacao.id_operacao] = operacao.valor
            case TipoNoLimite.DEBITAR:
                self.utilizado += operacao.valor
            case TipoNoLimite.CREDITAR | TipoNoLimite.ESTORNAR_DEBITO:
                self.utilizado -= operacao.valor
            case TipoNoLimite.LIBERAR_RESERVA:
                del self.holds[operacao.id_reserva or ""]
            case TipoNoLimite.CAPTURAR_RESERVA:
                del self.holds[operacao.id_reserva or ""]
                self.utilizado += operacao.valor
        self.aplicadas[operacao.id_operacao] = operacao
        self._versao += 1
        return ResultadoNoLimite(DesfechoNoLimite.APLICADA, self._saldos())

    def _recusa(self, id_contrato: str, operacao: OperacaoNoLimite) -> str | None:
        if operacao.id_conta is not None and operacao.id_conta != self.contas[id_contrato]:
            return "CONTA_DIVERGENTE"
        match operacao.tipo:
            case TipoNoLimite.RESERVAR | TipoNoLimite.DEBITAR:
                return "LIMITE_INSUFICIENTE" if self.disponivel < operacao.valor else None
            case TipoNoLimite.CREDITAR | TipoNoLimite.ESTORNAR_DEBITO:
                return "CREDITO_EXCEDE_UTILIZADO" if self.utilizado < operacao.valor else None
            case TipoNoLimite.LIBERAR_RESERVA | TipoNoLimite.CAPTURAR_RESERVA:
                gravado = self.holds.get(operacao.id_reserva or "")
                if gravado is None:
                    return "RESERVA_NAO_ENCONTRADA"
                esperado = operacao.valor_reservado or operacao.valor
                return "RESERVA_DIVERGENTE" if gravado != esperado else None
        return None

    def _saldos(self) -> SaldosDoLimite:
        assert self.utilizado >= 0 and self.reservado >= 0  # I2 do agregado real
        return SaldosDoLimite(
            self.concedido, self.utilizado, self.reservado, self.disponivel, self._versao
        )


def _corpo(valor: str = "150.00", **campos: object) -> bytes:
    """Corpo com o número escrito como texto: nunca passa por float no teste."""
    base: dict[str, object] = {"id_conta": "0001-1", "moeda": "BRL", "tipo_operacao": "RESERVA"}
    base.update(campos)
    texto = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in base.items())
    return f'{{{texto}, "valor": {valor}}}'.encode()


def _cabecalhos(chave: str | None = None, **extra: str) -> dict[str, str]:
    cabecalhos = {
        "Content-Type": "application/json",
        "X-Client-Id": "canal-pos",
        "Idempotency-Key": chave or str(uuid.uuid4()),
    }
    cabecalhos.update(extra)
    return cabecalhos


@pytest.fixture
def limites() -> LimitesFalso:
    return LimitesFalso()


def _cliente(
    limites: LimitesFalso,
    max_em_voo: int = 100,
    repositorio: RepositorioDeAutorizacoesEmMemoria | None = None,
) -> TestClient:
    repositorio = repositorio or RepositorioDeAutorizacoesEmMemoria()
    caso = AutorizarTransacao(repositorio, limites, RelogioFixo(), IdsSequenciais())
    return TestClient(criar_app(caso, ConsultarAutorizacao(repositorio), max_em_voo))


@pytest.fixture
def cliente(limites: LimitesFalso) -> TestClient:
    return _cliente(limites)


def test_201_com_location_corpo_e_saldos_como_numero(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert resposta.headers["location"] == f"autorizacoes/{corpo['id_autorizacao']}"
    assert "idempotent-replayed" not in resposta.headers
    assert '"saldo_reservado": 150.00' in resposta.text  # número cru, não string nem 150.0
    assert '"limite_disponivel": 850.00' in resposta.text
    assert corpo["status"] == "APROVADA"
    assert corpo["tipo_operacao"] == "RESERVA"
    assert corpo["id_contrato"] == CTR
    assert corpo["moeda"] == "BRL"
    assert corpo["versao_limite"] == 2
    assert corpo["criada_em"] == "2026-10-20T13:00:00.123Z"
    assert corpo["correlation_id"] == resposta.headers["x-correlation-id"]
    assert "id_autorizacao_original" not in corpo


def test_replay_200_com_corpo_identico_byte_a_byte(cliente: TestClient) -> None:
    cabecalhos = _cabecalhos()
    primeira = cliente.post(URL, content=_corpo(), headers=cabecalhos)

    replay = cliente.post(
        URL, content=_corpo("150"), headers={**cabecalhos, "X-Correlation-Id": str(uuid.uuid4())}
    )

    assert primeira.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.headers["location"] == primeira.headers["location"]
    assert replay.content == primeira.content  # inclusive o correlation_id ORIGINAL
    assert replay.headers["x-correlation-id"] != primeira.headers["x-correlation-id"]


def test_mesma_chave_com_corpo_diferente_e_422(cliente: TestClient) -> None:
    cabecalhos = _cabecalhos()
    cliente.post(URL, content=_corpo(), headers=cabecalhos)

    resposta = cliente.post(URL, content=_corpo("151.00"), headers=cabecalhos)

    assert resposta.status_code == 422
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.json()["codigo"] == "IDEMPOTENCY_KEY_REUTILIZADA"


def test_chave_entre_aspas_e_aceita_e_equivale_a_sem_aspas(cliente: TestClient) -> None:
    chave = str(uuid.uuid4())
    primeira = cliente.post(URL, content=_corpo(), headers=_cabecalhos(chave))

    replay = cliente.post(URL, content=_corpo(), headers=_cabecalhos(f'"{chave}"'))

    assert primeira.status_code == 201
    assert replay.status_code == 200
    assert replay.content == primeira.content


@pytest.mark.parametrize("chave", ["", "abc", "123", "{" + str(uuid.uuid4()) + "}"])
def test_chave_ausente_ou_invalida_e_400(cliente: TestClient, chave: str) -> None:
    cabecalhos = _cabecalhos()
    del cabecalhos["Idempotency-Key"]
    if chave:
        cabecalhos["Idempotency-Key"] = chave

    resposta = cliente.post(URL, content=_corpo(), headers=cabecalhos)

    assert resposta.status_code == 400
    assert resposta.json()["codigo"] == "IDEMPOTENCY_KEY_INVALIDA"


def test_json_malformado_e_400(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=b'{"valor": ', headers=_cabecalhos())

    assert resposta.status_code == 400
    assert resposta.json()["codigo"] == "REQUISICAO_MAL_FORMADA"


def test_sem_client_id_e_401_antes_de_qualquer_outra_validacao(cliente: TestClient) -> None:
    cabecalhos = _cabecalhos()
    del cabecalhos["X-Client-Id"]
    del cabecalhos["Idempotency-Key"]

    resposta = cliente.post(URL, content=b"lixo", headers=cabecalhos)

    assert resposta.status_code == 401
    assert resposta.json()["codigo"] == "NAO_AUTENTICADO"
    assert resposta.headers["x-correlation-id"]


def test_content_type_errado_e_415(cliente: TestClient) -> None:
    resposta = cliente.post(
        URL, content=_corpo(), headers=_cabecalhos(**{"Content-Type": "text/plain"})
    )

    assert resposta.status_code == 415
    assert resposta.json()["codigo"] == "TIPO_DE_MIDIA_NAO_SUPORTADO"


def test_content_type_com_charset_e_aceito(cliente: TestClient) -> None:
    resposta = cliente.post(
        URL,
        content=_corpo(),
        headers=_cabecalhos(**{"Content-Type": "application/json; charset=utf-8"}),
    )

    assert resposta.status_code == 201


def test_valor_com_tres_casas_e_422(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo("10.001"), headers=_cabecalhos())

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert corpo["erros"][0]["campo"] == "valor"


@pytest.mark.parametrize("valor", ['"150.00"', "true", "null"])
def test_valor_que_nao_e_numero_json_e_422(cliente: TestClient, valor: str) -> None:
    resposta = cliente.post(URL, content=_corpo(valor), headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["codigo"] == "VALIDACAO_DE_DOMINIO"


def test_metadata_com_chave_extra_e_422(cliente: TestClient) -> None:
    corpo = _corpo(metadata={"canal": "POS", "cpf": "123"})

    resposta = cliente.post(URL, content=corpo, headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == "metadata.cpf"


def test_metadata_valida_com_geo_e_aceita(cliente: TestClient) -> None:
    corpo = _corpo(
        metadata={"canal": "POS", "geo": {"uf": "SP", "municipio_ibge": "3509502"}},
        id_estabelecimento="EST-1",
    )

    assert cliente.post(URL, content=corpo, headers=_cabecalhos()).status_code == 201


def test_campo_desconhecido_no_corpo_e_422(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(extra="x"), headers=_cabecalhos())

    assert resposta.status_code == 422


def test_id_contrato_do_corpo_divergente_do_path_e_422(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(id_contrato="CTR-2"), headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == "id_contrato"


def test_moeda_diferente_de_brl_e_422_de_dominio(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(moeda="USD"), headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == "moeda"


def test_402_limite_insuficiente_e_replay_devolve_o_mesmo_402(cliente: TestClient) -> None:
    cabecalhos = _cabecalhos()

    primeira = cliente.post(URL, content=_corpo("1000.01"), headers=cabecalhos)
    replay = cliente.post(URL, content=_corpo("1000.01"), headers=cabecalhos)

    assert primeira.status_code == 402
    assert primeira.headers["content-type"] == PROBLEM_JSON
    corpo = primeira.json()
    assert corpo["codigo"] == "LIMITE_INSUFICIENTE"
    assert corpo["id_autorizacao"]
    assert '"limite_disponivel": 1000.00' in primeira.text
    assert replay.status_code == 402
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id_autorizacao"] == corpo["id_autorizacao"]


def test_contrato_inexistente_e_404(limites: LimitesFalso) -> None:
    cliente = _cliente(limites)

    resposta = cliente.post(
        "/v1/contratos/CTR-NAO/autorizacoes", content=_corpo(), headers=_cabecalhos()
    )

    assert resposta.status_code == 404
    assert resposta.json()["codigo"] == "CONTRATO_NAO_ENCONTRADO"


def test_limites_indisponivel_e_503_com_retry_after(
    cliente: TestClient, limites: LimitesFalso
) -> None:
    limites.indisponivel = True

    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())

    assert resposta.status_code == 503
    assert resposta.headers["retry-after"] == "1"
    assert resposta.json()["codigo"] == "SERVICO_INDISPONIVEL"


def test_mesma_chave_em_andamento_e_409_com_retry_after(
    cliente: TestClient, limites: LimitesFalso
) -> None:
    cabecalhos = _cabecalhos()
    limites.indisponivel = True
    cliente.post(URL, content=_corpo(), headers=cabecalhos)  # deixa EM_PROCESSAMENTO

    resposta = cliente.post(URL, content=_corpo(), headers=cabecalhos)

    assert resposta.status_code == 409
    assert resposta.json()["codigo"] == "REQUISICAO_EM_ANDAMENTO"
    assert int(resposta.headers["retry-after"]) >= 1


def test_estorno_201_e_segundo_estorno_409(cliente: TestClient, limites: LimitesFalso) -> None:
    original = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()
    estorno = _corpo(tipo_operacao="ESTORNO", id_autorizacao_original=original["id_autorizacao"])

    primeiro = cliente.post(URL, content=estorno, headers=_cabecalhos())
    segundo = cliente.post(URL, content=estorno, headers=_cabecalhos())

    assert primeiro.status_code == 201
    assert primeiro.json()["id_autorizacao_original"] == original["id_autorizacao"]
    assert primeiro.json()["tipo_operacao"] == "ESTORNO"
    assert limites.disponivel == Decimal("1000.00")
    assert segundo.status_code == 409
    assert segundo.json()["codigo"] == "CONFLITO_DE_ESTADO"


def test_estorno_de_original_inexistente_e_422(cliente: TestClient) -> None:
    estorno = _corpo(tipo_operacao="ESTORNO", id_autorizacao_original=str(uuid.uuid4()))

    resposta = cliente.post(URL, content=estorno, headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["codigo"] == "AUTORIZACAO_ORIGINAL_INVALIDA"


def test_estorno_sem_original_e_422_de_dominio(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(tipo_operacao="ESTORNO"), headers=_cabecalhos())

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == "id_autorizacao_original"


def test_get_200_reflete_o_estado_atual(cliente: TestClient) -> None:
    criada = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()
    cliente.post(
        URL,
        content=_corpo(tipo_operacao="ESTORNO", id_autorizacao_original=criada["id_autorizacao"]),
        headers=_cabecalhos(),
    )

    resposta = cliente.get(
        f"{URL}/{criada['id_autorizacao']}", headers={"X-Client-Id": "canal-pos"}
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "ESTORNADA"
    assert corpo["saldos"]["saldo_reservado"] == 150.0
    assert set(corpo["saldos"]) == {
        "saldo_reservado",
        "limite_disponivel",
        "limite_utilizado",
        "versao_limite",
    }
    assert corpo["atualizada_em"] == "2026-10-20T13:00:00.123Z"


def test_get_404_para_inexistente_ou_de_outro_contrato(cliente: TestClient) -> None:
    criada = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()
    cabecalhos = {"X-Client-Id": "canal-pos"}

    inexistente = cliente.get(f"{URL}/{uuid.uuid4()}", headers=cabecalhos)
    outro_contrato = cliente.get(
        f"/v1/contratos/CTR-2/autorizacoes/{criada['id_autorizacao']}", headers=cabecalhos
    )

    assert inexistente.status_code == outro_contrato.status_code == 404
    assert inexistente.json()["codigo"] == "AUTORIZACAO_NAO_ENCONTRADA"


def test_get_de_outro_canal_e_404_sem_revelar_que_existe(cliente: TestClient) -> None:
    """BOLA: saber o UUID não basta; só o canal que criou (client_id do token) enxerga a autorização."""
    criada = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()

    intruso = cliente.get(
        f"{URL}/{criada['id_autorizacao']}", headers={"X-Client-Id": "canal-intruso"}
    )
    dono = cliente.get(f"{URL}/{criada['id_autorizacao']}", headers={"X-Client-Id": "canal-pos"})

    assert intruso.status_code == 404
    assert intruso.json()["codigo"] == "AUTORIZACAO_NAO_ENCONTRADA"
    assert dono.status_code == 200


def test_get_sem_client_id_e_401(cliente: TestClient) -> None:
    assert cliente.get(f"{URL}/{uuid.uuid4()}").status_code == 401


def test_saude(cliente: TestClient) -> None:
    resposta = cliente.get("/saude")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}


def test_load_shedding_acima_do_maximo_em_voo_e_503(limites: LimitesFalso) -> None:
    cliente = _cliente(limites, max_em_voo=0)

    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())

    assert resposta.status_code == 503
    assert resposta.headers["retry-after"] == "1"
    assert resposta.headers["x-correlation-id"]
    assert resposta.json()["codigo"] == "SERVICO_INDISPONIVEL"
    assert limites.aplicadas == {}  # não processou
    assert cliente.get("/saude").status_code == 200  # healthcheck não é derrubado


def test_correlation_id_valido_e_ecoado_e_invalido_e_substituido(cliente: TestClient) -> None:
    valido = str(uuid.uuid4())

    eco = cliente.get("/saude", headers={"X-Correlation-Id": valido})
    trocado = cliente.get("/saude", headers={"X-Correlation-Id": "lixo\r\nx"})

    assert eco.headers["x-correlation-id"] == valido
    assert trocado.headers["x-correlation-id"] != "lixo"
    assert len(trocado.headers["x-correlation-id"]) == 36


def test_problema_traz_trace_id_do_traceparent(cliente: TestClient) -> None:
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    resposta = cliente.post(URL, content=b"{", headers=_cabecalhos(traceparent=tp))

    assert resposta.json()["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_valor_grande_sai_identico(cliente: TestClient) -> None:
    class Folgado(LimitesFalso):
        pass

    limites = Folgado(concedido=Decimal("99999999999999.99"))
    cliente = _cliente(limites)

    resposta = cliente.post(URL, content=_corpo("12345678901234.56"), headers=_cabecalhos())

    assert resposta.status_code == 201
    assert '"valor": 12345678901234.56' in resposta.text
    assert '"saldo_reservado": 999999999999999.99' not in resposta.text
    assert "12345678901234.56" in resposta.text


def test_erro_inesperado_vira_500_sem_vazar_detalhe(limites: LimitesFalso) -> None:
    def explode(*_: object) -> ResultadoNoLimite:
        raise RuntimeError("segredo interno")

    limites.aplicar = explode  # type: ignore[method-assign]
    cliente = _cliente(limites)

    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())

    assert resposta.status_code == 500
    assert resposta.json()["codigo"] == "ERRO_INTERNO"
    assert "segredo" not in resposta.text


@pytest.mark.parametrize("caminho", ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"])
def test_spec_gerada_pelo_fastapi_nao_e_publicada(cliente: TestClient, caminho: str) -> None:
    """O contrato é contracts/openapi/autorizacao-v1.yaml; a spec gerada divergia dele."""
    assert cliente.get(caminho).status_code == 404


GATEWAY_REQUEST_ID = "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"


def test_gateway_request_id_chega_ao_contexto_e_ao_log_da_requisicao(
    limites: LimitesFalso, caplog: pytest.LogCaptureFixture
) -> None:
    """Junção borda × serviço: o `$context.requestId` do gateway vai para o log e para os adapters."""
    vistos: list[str | None] = []

    def explode(*_: object) -> ResultadoNoLimite:
        vistos.append(gateway_request_id_atual.get())  # adapter de saída enxerga o contexto
        raise RuntimeError("falha no meio da requisição")

    limites.aplicar = explode  # type: ignore[method-assign]
    cliente = _cliente(limites)

    with caplog.at_level(logging.ERROR, logger="autorizacao.adapters.inbound.http"):
        resposta = cliente.post(
            URL,
            content=_corpo(),
            headers=_cabecalhos(**{"X-Gateway-Request-Id": GATEWAY_REQUEST_ID}),
        )

    assert resposta.status_code == 500
    assert vistos == [GATEWAY_REQUEST_ID]
    (registro,) = [r for r in caplog.records if r.getMessage() == "erro inesperado"]
    assert registro.__dict__["gateway_request_id"] == GATEWAY_REQUEST_ID
    assert registro.__dict__["correlation_id"] == resposta.headers["x-correlation-id"]
    assert gateway_request_id_atual.get() is None  # o contexto não vaza para fora da requisição


@pytest.mark.parametrize("valor", ["lixo\r\nforjado", "x" * 65, ""])
def test_gateway_request_id_fora_do_formato_e_ignorado(
    limites: LimitesFalso, caplog: pytest.LogCaptureFixture, valor: str
) -> None:
    def explode(*_: object) -> ResultadoNoLimite:
        raise RuntimeError("falha")

    limites.aplicar = explode  # type: ignore[method-assign]
    cliente = _cliente(limites)

    with caplog.at_level(logging.ERROR, logger="autorizacao.adapters.inbound.http"):
        cliente.post(URL, content=_corpo(), headers=_cabecalhos(**{"X-Gateway-Request-Id": valor}))

    (registro,) = [r for r in caplog.records if r.getMessage() == "erro inesperado"]
    assert "gateway_request_id" not in registro.__dict__
    assert registro.__dict__["correlation_id"]


# --- painel 6 -------------------------------------------------------------------------------------

SCHEMAS_DE_EVENTO = Path(__file__).resolve().parents[4] / "contracts" / "events"
TP_VALIDO = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _evento_publicado(repositorio: RepositorioDeAutorizacoesEmMemoria) -> dict[str, Any]:
    """O CloudEvent que o outbox publicaria para a TransacaoAutorizada, já validado contra o schema."""
    (evento,) = [e for e in repositorio.outbox if isinstance(e, TransacaoAutorizada)]
    publicado: dict[str, Any] = json.loads(serializar(para_cloudevent(evento)))
    schema = json.loads((SCHEMAS_DE_EVENTO / "transacao-autorizada.v1.schema.json").read_text())
    validador = jsonschema.Draft4Validator(
        schema, format_checker=jsonschema.Draft4Validator.FORMAT_CHECKER
    )
    assert not [e.message for e in validador.iter_errors(publicado)]
    return publicado


@pytest.mark.parametrize(
    "hostil",
    [
        "X",  # sem hífen: derrubava o concluir depois do limite aplicado (500 com hold preso)
        "a-DEADBEEF<script>alert(1)</script>",  # 2 partes: ia para dentro do CloudEvent publicado
        "<script>alert(1)</script>",
        "-".join(["00"] * 80),  # 80 partes
    ],
)
def test_traceparent_hostil_e_ignorado_201_e_evento_no_schema(
    limites: LimitesFalso, hostil: str
) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()
    cliente = _cliente(limites, repositorio=repositorio)

    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos(traceparent=hostil))

    assert resposta.status_code == 201
    gravada = repositorio.obter(resposta.json()["id_autorizacao"])
    assert gravada is not None and gravada.traceparent is None
    publicado = _evento_publicado(repositorio)
    assert "traceparent" not in publicado
    assert publicado["data"]["trace_id"] is None


def test_traceparent_valido_vai_ate_o_evento(limites: LimitesFalso) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()
    cliente = _cliente(limites, repositorio=repositorio)

    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos(traceparent=TP_VALIDO))

    assert resposta.status_code == 201
    publicado = _evento_publicado(repositorio)
    assert publicado["traceparent"] == TP_VALIDO
    assert publicado["data"]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_canal_do_metadata_vai_no_evento(limites: LimitesFalso) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()
    cliente = _cliente(limites, repositorio=repositorio)

    cliente.post(URL, content=_corpo(metadata={"canal": "POS"}), headers=_cabecalhos())

    assert _evento_publicado(repositorio)["data"]["canal"] == "POS"


def test_location_relativo_resolve_com_e_sem_prefixo_de_stage(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())
    id_aut = resposta.json()["id_autorizacao"]
    location = resposta.headers["location"]
    gateway = (
        "https://abc.execute-api.sa-east-1.amazonaws.com/v1"  # stage `v1` antes do /v1 do path
    )

    local = urljoin(str(resposta.request.url), location)
    atras_do_stage = urljoin(f"{gateway}{URL}", location)

    assert local == f"http://testserver{URL}/{id_aut}"
    assert atras_do_stage == f"{gateway}{URL}/{id_aut}"
    assert cliente.get(local, headers={"X-Client-Id": "canal-pos"}).status_code == 200


def test_armazenamento_indisponivel_e_503_com_retry_after(limites: LimitesFalso) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()

    def estrangulado(*_: object) -> None:
        raise ArmazenamentoIndisponivel("registrar")

    repositorio.registrar = estrangulado  # type: ignore[method-assign,assignment]
    repositorio.obter = estrangulado  # type: ignore[method-assign,assignment]
    cliente = _cliente(limites, repositorio=repositorio)

    post = cliente.post(URL, content=_corpo(), headers=_cabecalhos())
    get = cliente.get(f"{URL}/{uuid.uuid4()}", headers={"X-Client-Id": "canal-pos"})

    for resposta in (post, get):
        assert resposta.status_code == 503
        assert resposta.headers["retry-after"] == "1"
        assert resposta.json()["codigo"] == "SERVICO_INDISPONIVEL"
    assert "mesma Idempotency-Key" in post.json()["detail"]


def test_metodo_nao_suportado_e_405_em_problem_json(cliente: TestClient) -> None:
    resposta = cliente.delete(URL, headers=_cabecalhos())

    assert resposta.status_code == 405
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.headers["allow"] == "POST"
    assert resposta.json()["codigo"] == "METODO_NAO_PERMITIDO"
    assert resposta.json()["correlation_id"] == resposta.headers["x-correlation-id"]


def test_rota_inexistente_e_404_em_problem_json(cliente: TestClient) -> None:
    resposta = cliente.get("/v2/contratos/CTR-1/autorizacoes", headers=_cabecalhos())

    assert resposta.status_code == 404
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.json()["codigo"] == "RECURSO_NAO_ENCONTRADO"


@pytest.mark.parametrize("id_autorizacao", ["nao-e-uuid", "a" * 2100])
def test_get_com_id_fora_do_formato_uuid_e_422_nunca_500(
    cliente: TestClient, id_autorizacao: str
) -> None:
    resposta = cliente.get(f"{URL}/{id_autorizacao}", headers={"X-Client-Id": "canal-pos"})

    assert resposta.status_code == 422
    assert resposta.json()["codigo"] == "VALIDACAO_DE_DOMINIO"


def test_get_com_uuid_maiusculo_acha_a_autorizacao(cliente: TestClient) -> None:
    criada = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()

    resposta = cliente.get(
        f"{URL}/{criada['id_autorizacao'].upper()}", headers={"X-Client-Id": "canal-pos"}
    )

    assert resposta.status_code == 200


@pytest.mark.parametrize("constante", ["NaN", "Infinity", "-Infinity"])
def test_nan_e_infinity_nao_sao_json_e_400(cliente: TestClient, constante: str) -> None:
    resposta = cliente.post(URL, content=_corpo(constante), headers=_cabecalhos())

    assert resposta.status_code == 400
    assert resposta.json()["codigo"] == "REQUISICAO_MAL_FORMADA"


@pytest.mark.parametrize("client_id", ["x" * 65, 'canal"; DROP--', "canal pos", "a/b", "a:b"])
def test_client_id_fora_do_formato_e_401(cliente: TestClient, client_id: str) -> None:
    resposta = cliente.post(
        URL, content=_corpo(), headers=_cabecalhos(**{"X-Client-Id": client_id})
    )

    assert resposta.status_code == 401
    assert resposta.json()["codigo"] == "NAO_AUTENTICADO"


def test_client_id_do_cognito_e_dos_canais_locais_passa(cliente: TestClient) -> None:
    for client_id in ("4p1k2n3m5o6q7r8s9t0u1v2w3x", "canal-pos", "canal_app.v2"):
        resposta = cliente.post(
            URL, content=_corpo(), headers=_cabecalhos(**{"X-Client-Id": client_id})
        )
        assert resposta.status_code == 201, client_id


def test_422_corta_erros_em_20_e_diz_quantos_omitiu(cliente: TestClient) -> None:
    extras = {f"campo_{i}": i for i in range(50)}

    resposta = cliente.post(URL, content=_corpo(**extras), headers=_cabecalhos())

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert len(corpo["erros"]) == MAX_ERROS_POR_PROBLEMA == 20
    assert corpo["erros_omitidos"] == 30


def test_422_com_poucos_erros_nao_traz_erros_omitidos(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(extra="x"), headers=_cabecalhos())

    assert "erros_omitidos" not in resposta.json()


def test_replay_da_recusa_tem_corpo_identico_e_header_desta_requisicao(
    cliente: TestClient,
) -> None:
    cabecalhos = _cabecalhos(traceparent=TP_VALIDO)
    primeira = cliente.post(URL, content=_corpo("1000.01"), headers=cabecalhos)
    outro_trace = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    replay = cliente.post(
        URL,
        content=_corpo("1000.01"),
        headers={**cabecalhos, "X-Correlation-Id": str(uuid.uuid4()), "traceparent": outro_trace},
    )

    assert replay.status_code == primeira.status_code == 402
    assert replay.content == primeira.content  # correlation_id e trace_id da execução original
    assert replay.headers["x-correlation-id"] != primeira.headers["x-correlation-id"]
    assert replay.json()["correlation_id"] == primeira.headers["x-correlation-id"]


def test_estorno_sem_utilizado_a_devolver_e_422_com_texto_dos_dois_casos(
    cliente: TestClient,
) -> None:
    """DEBITO 50, CREDITO 50 (pagou), ESTORNO do débito: não há utilizado; o modelo não tem saldo credor."""
    debito = cliente.post(
        URL, content=_corpo("50.00", tipo_operacao="DEBITO"), headers=_cabecalhos()
    ).json()
    cliente.post(URL, content=_corpo("50.00", tipo_operacao="CREDITO"), headers=_cabecalhos())

    estorno = cliente.post(
        URL,
        content=_corpo(
            "50.00", tipo_operacao="ESTORNO", id_autorizacao_original=debito["id_autorizacao"]
        ),
        headers=_cabecalhos(),
    )

    assert estorno.status_code == 422
    problema = estorno.json()
    assert problema["codigo"] == "CREDITO_EXCEDE_UTILIZADO"
    assert "ESTORNO" in problema["detail"] and "CREDITO" in problema["detail"]


def test_conta_divergente_e_422_registrada_sem_expor_saldo_do_contrato_alheio(
    limites: LimitesFalso,
) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()
    cliente = _cliente(limites, repositorio=repositorio)
    cabecalhos = _cabecalhos()

    resposta = cliente.post(URL, content=_corpo(id_conta="9999-9"), headers=cabecalhos)
    replay = cliente.post(URL, content=_corpo(id_conta="9999-9"), headers=cabecalhos)
    consulta = cliente.get(
        f"{URL}/{_id_gravado(repositorio)}", headers={"X-Client-Id": "canal-pos"}
    )

    assert resposta.status_code == 422
    problema = resposta.json()
    assert problema["codigo"] == "CONTA_DIVERGENTE"
    assert problema["detail"] == "A conta informada não é a titular do contrato."
    assert "limite_disponivel" not in problema and "saldo_reservado" not in problema
    assert replay.status_code == 422 and replay.headers["idempotent-replayed"] == "true"
    assert consulta.status_code == 200
    assert consulta.json()["motivo_recusa"] == "CONTA_DIVERGENTE"
    assert "saldos" not in consulta.json()
    (recusa,) = [e for e in repositorio.outbox if isinstance(e, TransacaoRecusada)]
    dados = json.loads(serializar(para_cloudevent(recusa)))["data"]
    assert "limite_disponivel" not in dados and "versao_limite" not in dados
    assert limites.disponivel == Decimal("1000.00")


def _id_gravado(repositorio: RepositorioDeAutorizacoesEmMemoria) -> str:
    (evento,) = repositorio.outbox
    return str(evento.id_autorizacao)  # type: ignore[attr-defined]


def test_toda_operacao_pedida_pelo_canal_leva_o_id_conta_ao_limite(
    cliente: TestClient, limites: LimitesFalso
) -> None:
    reserva = cliente.post(URL, content=_corpo(), headers=_cabecalhos()).json()
    cliente.post(
        URL,
        content=_corpo(tipo_operacao="ESTORNO", id_autorizacao_original=reserva["id_autorizacao"]),
        headers=_cabecalhos(),
    )

    assert [op.id_conta for op in limites.aplicadas.values()] == ["0001-1", "0001-1"]


# --- exaustividade dos mapas de recusa (C1) ------------------------------------------------------


def test_todo_motivo_de_recusa_tem_codigo_e_todo_codigo_tem_problema() -> None:
    assert set(MotivoRecusa) == set(_CODIGO_DA_RECUSA)
    assert set(_CODIGO_DA_RECUSA.values()) <= set(_PROBLEMAS)
    assert set(_DETALHE_DA_RECUSA) <= set(_PROBLEMAS)


def test_todo_motivo_de_recusa_tem_detalhe() -> None:
    sem_detalhe = [
        m
        for m in MotivoRecusa
        if m not in _DETALHE_DO_MOTIVO and _CODIGO_DA_RECUSA[m] not in _DETALHE_DA_RECUSA
    ]
    assert sem_detalhe == []
