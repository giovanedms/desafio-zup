"""Testes do adapter HTTP público sobre `criar_app` (repositório em memória, Limites falso)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

import pytest
from autorizacao.adapters.inbound.http import criar_app
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.consultar import ConsultarAutorizacao
from autorizacao.application.portas import (
    ContratoInexistente,
    DesfechoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
    TipoNoLimite,
)
from autorizacao.domain.eventos import SaldosDoLimite
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
        return f"ev-{next(self._contador)}"


@dataclass
class LimitesFalso:
    disponivel: Decimal = Decimal("1000.00")
    contratos: set[str] = field(default_factory=lambda: {CTR})
    indisponivel: bool = False
    aplicadas: dict[str, OperacaoNoLimite] = field(default_factory=dict)
    _versao: int = 1

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        if self.indisponivel:
            raise LimitesIndisponivel("circuit breaker aberto")
        if id_contrato not in self.contratos:
            raise ContratoInexistente(id_contrato)
        if operacao.id_operacao in self.aplicadas:
            return ResultadoNoLimite(DesfechoNoLimite.JA_APLICADA, self._saldos())
        if operacao.tipo in (TipoNoLimite.RESERVAR, TipoNoLimite.DEBITAR):
            if self.disponivel < operacao.valor:
                return ResultadoNoLimite(
                    DesfechoNoLimite.RECUSADA, self._saldos(), "LIMITE_INSUFICIENTE"
                )
            self.disponivel -= operacao.valor
        else:
            self.disponivel += operacao.valor
        self.aplicadas[operacao.id_operacao] = operacao
        self._versao += 1
        return ResultadoNoLimite(DesfechoNoLimite.APLICADA, self._saldos())

    def _saldos(self) -> SaldosDoLimite:
        return SaldosDoLimite(
            Decimal("1000.00"),
            Decimal("0.00"),
            Decimal("1000.00") - self.disponivel,
            self.disponivel,
            self._versao,
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


def _cliente(limites: LimitesFalso, max_em_voo: int = 100) -> TestClient:
    repositorio = RepositorioDeAutorizacoesEmMemoria()
    caso = AutorizarTransacao(repositorio, limites, RelogioFixo(), IdsSequenciais())
    return TestClient(criar_app(caso, ConsultarAutorizacao(repositorio), max_em_voo))


@pytest.fixture
def cliente(limites: LimitesFalso) -> TestClient:
    return _cliente(limites)


def test_201_com_location_corpo_e_saldos_como_numero(cliente: TestClient) -> None:
    resposta = cliente.post(URL, content=_corpo(), headers=_cabecalhos())

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert resposta.headers["location"] == f"{URL}/{corpo['id_autorizacao']}"
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

    limites = Folgado(disponivel=Decimal("99999999999999.99"))
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
