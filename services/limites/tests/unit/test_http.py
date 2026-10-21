"""Testes do adapter HTTP sobre `criar_app`, com repositório em memória e relógio fixo."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from limites.adapters.inbound.http import criar_app
from limites.adapters.outbound.dynamodb import ArmazenamentoIndisponivel, ContencaoPersistente
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.casos_de_uso import AplicarOperacaoNoLimite, ConsultarLimite
from limites.domain.limite import Limite, TipoProduto
from limites.domain.operacoes import OperacaoDeLimite
from limites.domain.resultados import ResultadoDaOperacao

CTR = "CTR-1"
OPERACOES = f"/interno/v1/contratos/{CTR}/operacoes"
PROBLEM_JSON = "application/problem+json"
AGORA = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class RelogioFixo:
    def agora(self) -> datetime:
        return AGORA


def _novo_id() -> str:
    return str(uuid.uuid4())


def _corpo(tipo: str, valor: str, id_operacao: str | None = None, **extras: str) -> bytes:
    """Corpo com o número escrito como texto: nunca passa por float no teste."""
    campos = {"id_operacao": id_operacao or _novo_id(), "tipo": tipo, **extras}
    texto = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in campos.items())
    return f'{{{texto}, "valor": {valor}}}'.encode()


@pytest.fixture
def repositorio() -> RepositorioDeLimitesEmMemoria:
    repo = RepositorioDeLimitesEmMemoria()
    repo.criar(Limite.novo(CTR, "CONTA-1", TipoProduto.CREDITO, Decimal("1000.00")))
    return repo


@pytest.fixture
def cliente(repositorio: RepositorioDeLimitesEmMemoria) -> TestClient:
    app = criar_app(
        AplicarOperacaoNoLimite(repositorio, RelogioFixo()), ConsultarLimite(repositorio)
    )
    return TestClient(app)


def test_reservar_aplicada_devolve_saldos_como_numero_json(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "150.00"))

    assert resposta.status_code == 200
    assert resposta.headers["content-type"] == "application/json"
    assert '"saldo_reservado": 150.00' in resposta.text  # número cru, não string nem 150.0
    assert '"limite_disponivel": 850.00' in resposta.text
    assert resposta.json() == {
        "resultado": "APLICADA",
        "saldos": {
            "limite_concedido": 1000.0,
            "limite_utilizado": 0.0,
            "saldo_reservado": 150.0,
            "limite_disponivel": 850.0,
            "versao": 2,
        },
    }


def test_repetir_a_mesma_operacao_devolve_ja_aplicada(cliente: TestClient) -> None:
    corpo = _corpo("RESERVAR", "150.00")

    primeira = cliente.post(OPERACOES, content=corpo)
    segunda = cliente.post(OPERACOES, content=corpo)

    assert primeira.json()["resultado"] == "APLICADA"
    assert segunda.status_code == 200
    assert segunda.json()["resultado"] == "JA_APLICADA"
    assert '"saldo_reservado": 150.00' in segunda.text  # não reaplicou


def test_acima_do_disponivel_e_recusada_com_motivo_e_status_200(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "1000.01"))

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["resultado"] == "RECUSADA"
    assert corpo["motivo"] == "LIMITE_INSUFICIENTE"
    assert '"limite_disponivel": 1000.00' in resposta.text


def test_debitar_creditar_e_estornar_debito(cliente: TestClient) -> None:
    debito = cliente.post(OPERACOES, content=_corpo("DEBITAR", "300.50"))
    credito = cliente.post(OPERACOES, content=_corpo("CREDITAR", "100"))
    estorno = cliente.post(OPERACOES, content=_corpo("ESTORNAR_DEBITO", "200.50"))

    assert debito.json()["resultado"] == "APLICADA"
    assert credito.json()["resultado"] == "APLICADA"
    assert estorno.json()["resultado"] == "APLICADA"
    assert '"limite_utilizado": 0.00' in estorno.text
    assert '"limite_disponivel": 1000.00' in estorno.text


def test_liberar_reserva_desfaz_o_hold(cliente: TestClient) -> None:
    id_reserva = _novo_id()
    cliente.post(OPERACOES, content=_corpo("RESERVAR", "150.00", id_operacao=id_reserva))

    resposta = cliente.post(
        OPERACOES, content=_corpo("LIBERAR_RESERVA", "150.00", id_reserva=id_reserva)
    )

    assert resposta.json()["resultado"] == "APLICADA"
    assert '"saldo_reservado": 0.00' in resposta.text
    assert '"limite_disponivel": 1000.00' in resposta.text


def test_contrato_inexistente_devolve_404_problem(cliente: TestClient) -> None:
    resposta = cliente.post(
        "/interno/v1/contratos/NAO-EXISTE/operacoes", content=_corpo("RESERVAR", "10.00")
    )

    assert resposta.status_code == 404
    assert resposta.headers["content-type"] == PROBLEM_JSON
    corpo = resposta.json()
    assert corpo["codigo"] == "CONTRATO_NAO_ENCONTRADO"
    assert corpo["type"] == "https://financas.example/problemas/contrato-nao-encontrado"
    assert corpo["status"] == 404
    assert corpo["correlation_id"] == resposta.headers["x-correlation-id"]
    assert corpo["trace_id"] is None


@pytest.mark.parametrize("corpo", [b"{nao e json", b"", b"\xff\xfe"])
def test_json_malformado_devolve_400(cliente: TestClient, corpo: bytes) -> None:
    resposta = cliente.post(OPERACOES, content=corpo)

    assert resposta.status_code == 400
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.json()["codigo"] == "REQUISICAO_MAL_FORMADA"


def test_valor_com_tres_casas_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "10.001"))

    assert resposta.status_code == 422
    assert resposta.headers["content-type"] == PROBLEM_JSON
    corpo = resposta.json()
    assert corpo["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert corpo["erros"][0]["campo"] == "valor"


@pytest.mark.parametrize("valor", ["0", "-5.00", '"abc"', "true", "null", "NaN"])
def test_valor_invalido_devolve_422(cliente: TestClient, valor: str) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", valor))

    assert resposta.status_code == 422
    assert resposta.json()["codigo"] == "VALIDACAO_DE_DOMINIO"


def test_campo_extra_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "10.00", moeda="BRL"))

    assert resposta.status_code == 422
    erros = resposta.json()["erros"]
    assert [e["campo"] for e in erros] == ["moeda"]


def test_tipo_desconhecido_e_id_invalido_devolvem_422(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("TRANSFERIR", "10.00", id_operacao="abc"))

    assert resposta.status_code == 422
    assert {e["campo"] for e in resposta.json()["erros"]} == {"id_operacao", "tipo"}


def test_corpo_que_nao_e_objeto_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=b"[1, 2]")

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == "corpo"


def test_liberar_reserva_sem_id_reserva_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("LIBERAR_RESERVA", "10.00"))

    assert resposta.status_code == 422
    assert resposta.json()["erros"] == [
        {"campo": "id_reserva", "mensagem": "obrigatório em LIBERAR_RESERVA"}
    ]


def test_validacao_vem_antes_do_404(cliente: TestClient) -> None:
    resposta = cliente.post("/interno/v1/contratos/NAO-EXISTE/operacoes", content=b"{")

    assert resposta.status_code == 400


@pytest.mark.parametrize(
    ("valor", "disponivel"),
    [
        ("12345678901234.56", "9987654321098765.43"),
        ("9007199254740993.01", "992800745259006.98"),  # 2**53 + 1: float perderia o último dígito
    ],
)
def test_valor_grande_sai_identico_sem_perda(
    cliente: TestClient, valor: str, disponivel: str
) -> None:
    repo = RepositorioDeLimitesEmMemoria()
    repo.criar(Limite.novo("CTR-G", "CONTA-G", TipoProduto.CREDITO, Decimal("9999999999999999.99")))
    app = criar_app(AplicarOperacaoNoLimite(repo, RelogioFixo()), ConsultarLimite(repo))

    resposta = TestClient(app).post(
        "/interno/v1/contratos/CTR-G/operacoes", content=_corpo("RESERVAR", valor)
    )

    assert resposta.status_code == 200
    assert f'"saldo_reservado": {valor}' in resposta.text
    assert f'"limite_disponivel": {disponivel}' in resposta.text
    assert '"limite_concedido": 9999999999999999.99' in resposta.text


def test_consultar_limite_devolve_o_schema_do_contrato(cliente: TestClient) -> None:
    cliente.post(OPERACOES, content=_corpo("RESERVAR", "150.00"))

    resposta = cliente.get(f"/interno/v1/contratos/{CTR}/limite")

    assert resposta.status_code == 200
    assert resposta.json() == {
        "id_contrato": CTR,
        "status_contrato": "ATIVO",
        "tipo_produto": "CREDITO",
        "limite_concedido": 1000.0,
        "limite_utilizado": 0.0,
        "saldo_reservado": 150.0,
        "limite_disponivel": 850.0,
        "versao": 2,
        "reservas_ativas": 1,
        "versao_legado": 0,
    }
    assert '"saldo_reservado": 150.00' in resposta.text
    assert "atualizado_pelo_legado_em" not in resposta.text


def test_consultar_limite_inexistente_devolve_404(cliente: TestClient) -> None:
    resposta = cliente.get("/interno/v1/contratos/NAO-EXISTE/limite")

    assert resposta.status_code == 404
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.json()["codigo"] == "CONTRATO_NAO_ENCONTRADO"


def test_id_contrato_fora_do_padrao_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.get("/interno/v1/contratos/id_com_underscore/limite")

    assert resposta.status_code == 422
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.json()["erros"][0]["campo"] == "id_contrato"


def test_saude(cliente: TestClient) -> None:
    resposta = cliente.get("/saude")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}


def test_correlation_id_valido_e_ecoado_em_resposta_e_problema(cliente: TestClient) -> None:
    correlacao = "3f2b8a52-6f0e-4b53-8c3b-0f6f3a7d9e11"

    ok = cliente.get("/saude", headers={"X-Correlation-Id": correlacao})
    problema = cliente.get(
        "/interno/v1/contratos/NAO-EXISTE/limite", headers={"X-Correlation-Id": correlacao}
    )

    assert ok.headers["x-correlation-id"] == correlacao
    assert problema.headers["x-correlation-id"] == correlacao
    assert problema.json()["correlation_id"] == correlacao


@pytest.mark.parametrize(
    "invalido",
    ["nao-e-uuid", "{3f2b8a52-6f0e-4b53-8c3b-0f6f3a7d9e11}", "3f2b8a526f0e4b538c3b0f6f3a7d9e11"],
)
def test_correlation_id_invalido_e_substituido_por_uuid_valido(
    cliente: TestClient, invalido: str
) -> None:
    resposta = cliente.get(
        "/interno/v1/contratos/NAO-EXISTE/limite", headers={"X-Correlation-Id": invalido}
    )

    gerado = resposta.headers["x-correlation-id"]
    assert gerado != invalido
    assert str(uuid.UUID(gerado)) == gerado
    assert resposta.json()["correlation_id"] == gerado


def test_correlation_id_ausente_gera_uuid(cliente: TestClient) -> None:
    gerado = cliente.get("/saude").headers["x-correlation-id"]

    assert str(uuid.UUID(gerado)) == gerado


def test_rota_inexistente_tambem_devolve_correlation_id(cliente: TestClient) -> None:
    resposta = cliente.get("/nao-existe")

    assert resposta.status_code == 404
    assert "x-correlation-id" in resposta.headers


def test_trace_id_vem_do_traceparent_valido(cliente: TestClient) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    resposta = cliente.get(
        "/interno/v1/contratos/NAO-EXISTE/limite", headers={"traceparent": traceparent}
    )

    assert resposta.json()["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_traceparent_invalido_vira_trace_id_nulo(cliente: TestClient) -> None:
    resposta = cliente.get(
        "/interno/v1/contratos/NAO-EXISTE/limite", headers={"traceparent": "lixo"}
    )

    assert "trace_id" in resposta.json()
    assert resposta.json()["trace_id"] is None


class _RepositorioQueFalha:
    """Aplica sempre falha com a exceção dada, para exercitar o mapeamento de erros."""

    def __init__(self, erro: Exception) -> None:
        self._erro = erro

    def aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        raise self._erro

    def obter(self, id_contrato: str) -> Limite | None:
        raise self._erro


def _cliente_que_falha(erro: Exception) -> TestClient:
    repo = _RepositorioQueFalha(erro)
    return TestClient(
        criar_app(AplicarOperacaoNoLimite(repo, RelogioFixo()), ConsultarLimite(repo))
    )


def test_contencao_persistente_devolve_503_com_retry_after() -> None:
    resposta = _cliente_que_falha(ContencaoPersistente(CTR)).post(
        OPERACOES, content=_corpo("RESERVAR", "10.00")
    )

    assert resposta.status_code == 503
    assert resposta.headers["content-type"] == PROBLEM_JSON
    assert resposta.headers["retry-after"] == "1"
    assert resposta.json()["codigo"] == "SERVICO_INDISPONIVEL"


def test_banco_estrangulado_devolve_503_com_retry_after_na_operacao_e_na_consulta() -> None:
    cliente = _cliente_que_falha(ArmazenamentoIndisponivel(CTR))

    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "10.00"))
    consulta = cliente.get(f"/interno/v1/contratos/{CTR}/limite")

    for r in (resposta, consulta):
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        assert r.json()["codigo"] == "SERVICO_INDISPONIVEL"


def test_captura_pela_api_interna(cliente: TestClient) -> None:
    id_reserva = _novo_id()
    cliente.post(OPERACOES, content=_corpo("RESERVAR", "400.00", id_operacao=id_reserva))

    resposta = cliente.post(
        OPERACOES,
        content=_corpo("CAPTURAR_RESERVA", "250.00", id_reserva=id_reserva)[:-1]
        + b', "valor_reservado": 400.00}',
    )

    assert resposta.status_code == 200
    assert resposta.json()["resultado"] == "APLICADA"
    assert resposta.json()["saldos"]["limite_utilizado"] == 250.00
    assert resposta.json()["saldos"]["saldo_reservado"] == 0.00


@pytest.mark.parametrize("faltando", ["id_reserva", "valor_reservado"])
def test_captura_sem_hold_identificado_devolve_422(cliente: TestClient, faltando: str) -> None:
    extras = {"id_reserva": _novo_id()} if faltando == "valor_reservado" else {}
    corpo = _corpo("CAPTURAR_RESERVA", "10.00", **extras)
    if faltando == "id_reserva":
        corpo = corpo[:-1] + b', "valor_reservado": 10.00}'

    resposta = cliente.post(OPERACOES, content=corpo)

    assert resposta.status_code == 422
    assert resposta.json()["erros"][0]["campo"] == faltando


def test_id_contrato_maior_que_o_layout_do_legado_devolve_422(cliente: TestClient) -> None:
    resposta = cliente.get(f"/interno/v1/contratos/{'C' * 21}/limite")

    assert resposta.status_code == 422


def test_excecao_inesperada_devolve_500_sem_vazar_detalhes() -> None:
    cliente = _cliente_que_falha(RuntimeError("senha-do-banco-no-stack"))

    resposta = cliente.post(OPERACOES, content=_corpo("RESERVAR", "10.00"))
    consulta = cliente.get(f"/interno/v1/contratos/{CTR}/limite")

    for r in (resposta, consulta):
        assert r.status_code == 500
        assert r.headers["content-type"] == PROBLEM_JSON
        assert r.json()["codigo"] == "ERRO_INTERNO"
        assert r.json()["correlation_id"] == r.headers["x-correlation-id"]
        assert "senha-do-banco" not in r.text
        assert "Traceback" not in r.text


def test_consulta_da_operacao_diz_guarda_e_hold_sem_escrever(
    cliente: TestClient, repositorio: RepositorioDeLimitesEmMemoria
) -> None:
    reserva, debito, desconhecida = _novo_id(), _novo_id(), _novo_id()
    cliente.post(OPERACOES, content=_corpo("RESERVAR", "100.00", id_operacao=reserva))
    cliente.post(OPERACOES, content=_corpo("DEBITAR", "50.00", id_operacao=debito))
    versao = repositorio.obter(CTR).versao  # type: ignore[union-attr]

    def consultar(id_op: str) -> dict[str, object]:
        resposta = cliente.get(f"/interno/v1/contratos/{CTR}/operacoes/{id_op}")
        assert resposta.status_code == 200
        return dict(resposta.json())

    assert consultar(reserva) | {"versao": 0} == {
        "id_operacao": reserva, "na_guarda": True, "reserva_ativa": True, "versao": 0,
    }  # fmt: skip
    assert consultar(debito)["na_guarda"] is True and consultar(debito)["reserva_ativa"] is False
    assert consultar(desconhecida)["na_guarda"] is False
    assert repositorio.obter(CTR).versao == versao  # type: ignore[union-attr]  # nada escrito


def test_consulta_de_contrato_inexistente_e_404(cliente: TestClient) -> None:
    resposta = cliente.get(f"/interno/v1/contratos/NAO-EXISTE/operacoes/{_novo_id()}")

    assert resposta.status_code == 404


def test_prazo_do_chamador_vencido_nao_escreve_e_devolve_503(
    cliente: TestClient, repositorio: RepositorioDeLimitesEmMemoria
) -> None:
    """O chamador já desistiu: aplicar agora seria uma escrita "atrasada" que o reconciliador não espera."""
    vencido = int(AGORA.timestamp() * 1000) - 1
    resposta = cliente.post(
        OPERACOES, content=_corpo("DEBITAR", "10.00"), headers={"X-Prazo-Ms": str(vencido)}
    )

    assert resposta.status_code == 503
    assert resposta.headers["retry-after"] == "1"
    assert repositorio.obter(CTR).utilizado == Decimal("0.00")  # type: ignore[union-attr]


def test_prazo_no_futuro_aplica_normalmente(cliente: TestClient) -> None:
    futuro = int(AGORA.timestamp() * 1000) + 2000
    resposta = cliente.post(
        OPERACOES, content=_corpo("DEBITAR", "10.00"), headers={"X-Prazo-Ms": str(futuro)}
    )

    assert resposta.status_code == 200
    assert resposta.json()["resultado"] == "APLICADA"


def test_conta_divergente_e_recusa_200_e_o_mesmo_id_com_a_conta_certa_aplica(
    cliente: TestClient,
) -> None:
    id_operacao = _novo_id()

    errada = cliente.post(
        OPERACOES, content=_corpo("DEBITAR", "10.00", id_operacao=id_operacao, id_conta="CONTA-2")
    )
    certa = cliente.post(
        OPERACOES, content=_corpo("DEBITAR", "10.00", id_operacao=id_operacao, id_conta="CONTA-1")
    )

    assert errada.status_code == 200
    assert (errada.json()["resultado"], errada.json()["motivo"]) == ("RECUSADA", "CONTA_DIVERGENTE")
    assert '"limite_utilizado": 0.00' in errada.text
    assert certa.json()["resultado"] == "APLICADA"


@pytest.mark.parametrize("conta", ["", "conta com espaco", "0" * 17, "0001_1"])
def test_id_conta_fora_do_padrao_devolve_422(cliente: TestClient, conta: str) -> None:
    resposta = cliente.post(OPERACOES, content=_corpo("DEBITAR", "10.00", id_conta=conta))

    assert resposta.status_code == 422
    assert [e["campo"] for e in resposta.json()["erros"]] == ["id_conta"]
