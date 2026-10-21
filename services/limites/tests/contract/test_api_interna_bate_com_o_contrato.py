"""Conformidade API interna ≡ contracts/openapi/limites-interno-v1.yaml (fronteira autorizacao → limites).

Duas metades, as duas obrigatórias numa fronteira entre BCs:
  - vocabulário: todo enum do contrato é exatamente o enum do domínio (motivo, resultado, tipo, status, produto).
    Motivo novo no domínio sem entrar no contrato (ou o inverso) quebra aqui, não no consumidor;
  - respostas: o app real, com o repositório em memória, responde cada desfecho com status DECLARADO na operação,
    media type declarado, corpo válido no schema (jsonschema Draft 4, o dialeto do OpenAPI 3.0), nenhum membro fora
    do schema e, nos problemas, `codigo`/`type`/`status` iguais à linha do catálogo (contracts/problem-types).
Mesmo padrão de services/autorizacao/tests/contract/test_api_bate_com_o_contrato.py (contrato público).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml  # PyYAML chega com uvicorn[standard], dependência do serviço
from fastapi.testclient import TestClient
from jsonschema import Draft4Validator, FormatChecker
from limites.adapters.inbound.http import criar_app
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.casos_de_uso import AplicarOperacaoNoLimite, ConsultarLimite
from limites.application.portas import ArmazenamentoIndisponivel
from limites.domain.limite import Limite, StatusContrato, TipoProduto
from limites.domain.operacoes import OperacaoDeLimite, TipoDeOperacao
from limites.domain.resultados import Desfecho, MotivoRecusa, ResultadoDaOperacao

RAIZ = Path(__file__).resolve().parents[4]
CONTRATO: dict[str, Any] = yaml.safe_load(
    (RAIZ / "contracts/openapi/limites-interno-v1.yaml").read_text(encoding="utf-8")
)
CATALOGO = (RAIZ / "contracts/problem-types/catalogo.md").read_text(encoding="utf-8")
POST = ("/interno/v1/contratos/{id_contrato}/operacoes", "post")
GET_OPERACAO = ("/interno/v1/contratos/{id_contrato}/operacoes/{id_operacao}", "get")
GET_LIMITE = ("/interno/v1/contratos/{id_contrato}/limite", "get")

CTR, CONTA = "CTR-1", "0001-12345678-9"
OPERACOES = f"/interno/v1/contratos/{CTR}/operacoes"
AGORA = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

FORMATOS = FormatChecker()


@FORMATOS.checks("uri")
def _uri(valor: object) -> bool:
    if not isinstance(valor, str):
        return True
    partes = urlsplit(valor)
    return bool(partes.scheme and partes.netloc)


def _no(ref: str) -> Any:
    no: Any = CONTRATO
    for parte in ref.removeprefix("#/").split("/"):
        no = no[int(parte)] if isinstance(no, list) else no[parte]
    return no


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


def _validar(corpo: Any, schema: dict[str, Any]) -> list[str]:
    validador = Draft4Validator(
        {**schema, "components": COMPONENTES}, format_checker=FORMATOS
    )  # `$ref: #/components/...` resolve na raiz; em Draft 4 o irmão `components` não valida nada
    return [f"{list(e.absolute_path)}: {e.message}" for e in validador.iter_errors(corpo)]


def _propriedades(schema: dict[str, Any]) -> set[str]:
    """Membros declarados, atravessando `$ref` e `allOf`."""
    if "$ref" in schema:
        return _propriedades(_no(schema["$ref"]))
    nomes = set(schema.get("properties", {}))
    for parte in schema.get("allOf", []):
        nomes |= _propriedades(parte)
    return nomes


def _linha_do_catalogo(codigo: str) -> tuple[str, int]:
    linha = re.search(rf"^\| {codigo} \| ([a-z-]+) \| (\d{{3}}) \|", CATALOGO, re.MULTILINE)
    assert linha, f"{codigo} não está no catálogo"
    return linha[1], int(linha[2])


def conferir(resposta: httpx.Response, operacao: tuple[str, str]) -> dict[str, Any]:
    """Falha se a resposta não for uma das declaradas na operação; devolve o corpo."""
    caminho, metodo = operacao
    declaradas = CONTRATO["paths"][caminho][metodo]["responses"]
    status = str(resposta.status_code)
    assert status in declaradas, f"{metodo.upper()} {caminho}: status {status} não declarado"
    declarada = declaradas[status]
    declarada = _no(declarada["$ref"]) if "$ref" in declarada else declarada
    ((media_type, conteudo),) = declarada["content"].items()
    assert resposta.headers["content-type"].split(";")[0] == media_type
    schema = _json_schema(conteudo["schema"])

    corpo = json.loads(resposta.content)
    erros = _validar(corpo, schema)
    assert not erros, f"corpo {status} fora do schema: {erros}"
    nao_declarados = set(corpo) - _propriedades(schema)
    assert not nao_declarados, (
        f"membros do corpo {status} não declarados no contrato: {nao_declarados}"
    )
    if "Retry-After" in declarada.get("headers", {}):
        assert int(resposta.headers["retry-after"]) >= 1
    if media_type == "application/problem+json":
        slug, status_no_catalogo = _linha_do_catalogo(corpo["codigo"])
        assert corpo["type"].endswith(f"/problemas/{slug}")
        assert corpo["status"] == status_no_catalogo == resposta.status_code
    return dict(corpo)


class RelogioFixo:
    def agora(self) -> datetime:
        return AGORA


class _RepositorioQueFalha:
    def __init__(self, erro: Exception) -> None:
        self._erro = erro

    def aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        raise self._erro

    def obter(self, id_contrato: str) -> Limite | None:
        raise self._erro


def _app(repositorio: Any) -> TestClient:
    return TestClient(
        criar_app(AplicarOperacaoNoLimite(repositorio, RelogioFixo()), ConsultarLimite(repositorio))
    )


@pytest.fixture
def cliente() -> TestClient:
    repositorio = RepositorioDeLimitesEmMemoria()
    repositorio.criar(Limite.novo(CTR, CONTA, TipoProduto.CREDITO, Decimal("1000.00")))
    return _app(repositorio)


def _corpo(tipo: str, valor: str = "100.00", **extras: str) -> bytes:
    """Número escrito como texto no JSON (nunca passa por float)."""
    campos = {"id_operacao": str(uuid.uuid4()), "tipo": tipo, **extras}
    texto = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in campos.items())
    return f'{{{texto}, "valor": {valor}}}'.encode()


# --- Vocabulário ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caminho", "dominio"),
    [
        ("ResultadoDaOperacao/properties/motivo", MotivoRecusa),
        ("ResultadoDaOperacao/properties/resultado", Desfecho),
        ("Operacao/properties/tipo", TipoDeOperacao),
        ("Limite/allOf/1/properties/status_contrato", StatusContrato),
        ("Limite/allOf/1/properties/tipo_produto", TipoProduto),
    ],
    ids=["motivo", "resultado", "tipo", "status_contrato", "tipo_produto"],
)
def test_enum_do_contrato_e_exatamente_o_do_dominio(caminho: str, dominio: type[StrEnum]) -> None:
    assert set(_no(f"#/components/schemas/{caminho}")["enum"]) == {m.value for m in dominio}


@pytest.mark.parametrize(
    "corpo",
    [
        _corpo("RESERVAR"),
        _corpo("DEBITAR", id_conta=CONTA),
        _corpo("CREDITAR", "0.01"),
        _corpo("ESTORNAR_DEBITO", id_conta=CONTA),
        _corpo("LIBERAR_RESERVA", id_reserva=str(uuid.uuid4()), id_conta=CONTA),
        _corpo("CAPTURAR_RESERVA", id_reserva=str(uuid.uuid4()))[:-1]
        + b', "valor_reservado": 100}',
    ],
    ids=lambda c: json.loads(c)["tipo"],
)
def test_corpo_que_o_app_aceita_vale_no_schema_operacao(cliente: TestClient, corpo: bytes) -> None:
    """O lado da requisição: o que o consumidor manda (inclusive `id_conta`) está declarado no contrato."""
    assert not _validar(json.loads(corpo), {"$ref": "#/components/schemas/Operacao"})
    assert cliente.post(OPERACOES, content=corpo).status_code == 200


# --- POST .../operacoes ----------------------------------------------------------------------------


def test_200_aplicada_ja_aplicada_e_recusada(cliente: TestClient) -> None:
    corpo = _corpo("RESERVAR", id_conta=CONTA)

    aplicada = conferir(cliente.post(OPERACOES, content=corpo), POST)
    ja_aplicada = conferir(cliente.post(OPERACOES, content=corpo), POST)
    insuficiente = conferir(cliente.post(OPERACOES, content=_corpo("DEBITAR", "5000.00")), POST)
    outra_conta = conferir(
        cliente.post(OPERACOES, content=_corpo("DEBITAR", id_conta="0002-1")), POST
    )

    assert aplicada["resultado"] == "APLICADA" and "motivo" not in aplicada
    assert ja_aplicada["resultado"] == "JA_APLICADA" and "motivo" not in ja_aplicada
    assert (insuficiente["resultado"], insuficiente["motivo"]) == (
        "RECUSADA",
        "LIMITE_INSUFICIENTE",
    )
    assert (outra_conta["resultado"], outra_conta["motivo"]) == ("RECUSADA", "CONTA_DIVERGENTE")


def test_400_e_404(cliente: TestClient) -> None:
    malformado = cliente.post(OPERACOES, content=b'{"valor": ')
    nome_repetido = cliente.post(OPERACOES, content=_corpo("RESERVAR")[:-1] + b', "valor": 999}')
    inexistente = cliente.post(
        "/interno/v1/contratos/NAO-EXISTE/operacoes", content=_corpo("RESERVAR")
    )

    assert conferir(malformado, POST)["codigo"] == "REQUISICAO_MAL_FORMADA"
    assert conferir(nome_repetido, POST)["codigo"] == "REQUISICAO_MAL_FORMADA"
    assert conferir(inexistente, POST)["codigo"] == "CONTRATO_NAO_ENCONTRADO"


INVALIDAS = {
    "valor zero": _corpo("RESERVAR", "0"),
    "valor com 3 casas": _corpo("RESERVAR", "10.001"),
    "tipo desconhecido": _corpo("TRANSFERIR"),
    "campo desconhecido": _corpo("RESERVAR", moeda="BRL"),
    "id_conta fora do pattern": _corpo("RESERVAR", id_conta="conta com espaco"),
    "liberar sem id_reserva": _corpo("LIBERAR_RESERVA"),
    "JSON que não é objeto": b"[]",
    # `erros` cortado em 20 (maxItems) e `erros_omitidos` declarado no Problema
    "30 campos desconhecidos": _corpo("RESERVAR", **{f"extra{i}": "x" for i in range(30)}),
}


@pytest.mark.parametrize("caso", INVALIDAS)
def test_422_operacao_invalida(cliente: TestClient, caso: str) -> None:
    resposta = cliente.post(OPERACOES, content=INVALIDAS[caso])

    assert resposta.status_code == 422, caso
    problema = conferir(resposta, POST)
    assert problema["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert problema["erros"]


def test_503_prazo_vencido_e_banco_estrangulado_e_500(cliente: TestClient) -> None:
    vencido = str(int(AGORA.timestamp() * 1000) - 1)
    prazo = cliente.post(OPERACOES, content=_corpo("DEBITAR"), headers={"X-Prazo-Ms": vencido})
    estrangulado = _app(_RepositorioQueFalha(ArmazenamentoIndisponivel(CTR))).post(
        OPERACOES, content=_corpo("DEBITAR")
    )
    bug = _app(_RepositorioQueFalha(RuntimeError("falha"))).post(
        OPERACOES, content=_corpo("DEBITAR")
    )

    assert conferir(prazo, POST)["codigo"] == "SERVICO_INDISPONIVEL"
    assert conferir(estrangulado, POST)["codigo"] == "SERVICO_INDISPONIVEL"
    assert conferir(bug, POST)["codigo"] == "ERRO_INTERNO"


# --- GET .../operacoes/{id} e GET .../limite -------------------------------------------------------


def test_consulta_da_operacao_200_404_422_503(cliente: TestClient) -> None:
    id_reserva = str(uuid.uuid4())
    cliente.post(OPERACOES, content=_corpo("RESERVAR", id_operacao=id_reserva))
    falha = _app(_RepositorioQueFalha(ArmazenamentoIndisponivel(CTR)))

    encontrada = cliente.get(f"{OPERACOES}/{id_reserva}")
    inexistente = cliente.get(f"/interno/v1/contratos/NAO-EXISTE/operacoes/{uuid.uuid4()}")
    invalida = cliente.get(f"{OPERACOES}/nao-e-uuid")
    indisponivel = falha.get(f"{OPERACOES}/{id_reserva}")

    assert conferir(encontrada, GET_OPERACAO) == {
        "id_operacao": id_reserva,
        "na_guarda": True,
        "reserva_ativa": True,
        "versao": 2,
    }
    assert conferir(inexistente, GET_OPERACAO)["codigo"] == "CONTRATO_NAO_ENCONTRADO"
    assert conferir(invalida, GET_OPERACAO)["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert conferir(indisponivel, GET_OPERACAO)["codigo"] == "SERVICO_INDISPONIVEL"


def test_consulta_do_limite_200_404_422_503(cliente: TestClient) -> None:
    falha = _app(_RepositorioQueFalha(ArmazenamentoIndisponivel(CTR)))

    limite = conferir(cliente.get(f"/interno/v1/contratos/{CTR}/limite"), GET_LIMITE)
    inexistente = cliente.get("/interno/v1/contratos/NAO-EXISTE/limite")
    invalido = cliente.get(f"/interno/v1/contratos/{'C' * 21}/limite")
    indisponivel = falha.get(f"/interno/v1/contratos/{CTR}/limite")

    assert limite["limite_disponivel"] == 1000
    assert conferir(inexistente, GET_LIMITE)["codigo"] == "CONTRATO_NAO_ENCONTRADO"
    assert conferir(invalido, GET_LIMITE)["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert conferir(indisponivel, GET_LIMITE)["codigo"] == "SERVICO_INDISPONIVEL"


def test_o_conferidor_reprova_corpo_fora_do_contrato() -> None:
    """Sanidade do próprio teste: motivo fora do enum e membro extra não passam."""
    ruim = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps({"resultado": "RECUSADA", "motivo": "INVENTADO", "saldos": {}}).encode(),
    )

    with pytest.raises(AssertionError, match="fora do schema"):
        conferir(ruim, POST)
