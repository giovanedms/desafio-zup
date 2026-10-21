"""Conformidade API ≡ OpenAPI: o app real responde o que contracts/openapi/autorizacao-v1.yaml declara.

O app sobe com os dublês dos testes HTTP (repositório em memória, Limites falso) e cada resposta é conferida
contra a resposta DECLARADA para aquele status na operação: status previsto, media type, corpo válido no
schema (jsonschema, Draft 4, que é o dialeto do OpenAPI 3.0), todo membro do corpo declarado no schema e,
nos problemas, `codigo`/`type`/`status` iguais à linha do catálogo (contracts/problem-types/catalogo.md).
Sem Schemathesis: os cenários são escolhidos, um por desfecho que o enunciado pede.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml  # PyYAML chega com uvicorn[standard], dependência do serviço
from autorizacao.adapters.inbound.http import _PROBLEMAS
from autorizacao.adapters.outbound.memoria import RepositorioDeAutorizacoesEmMemoria
from autorizacao.application.portas import ArmazenamentoIndisponivel
from autorizacao.domain.autorizacao import MotivoRecusa
from jsonschema import Draft4Validator, FormatChecker

RAIZ = Path(__file__).resolve().parents[4]
CONTRATO: dict[str, Any] = yaml.safe_load(
    (RAIZ / "contracts/openapi/autorizacao-v1.yaml").read_text(encoding="utf-8")
)
CATALOGO = (RAIZ / "contracts/problem-types/catalogo.md").read_text(encoding="utf-8")
POST = ("/v1/contratos/{id_contrato}/autorizacoes", "post")
GET = ("/v1/contratos/{id_contrato}/autorizacoes/{id_autorizacao}", "get")

# Dublês e helpers dos testes HTTP, carregados pelo caminho (mesmo padrão de tests/contract/test_compat_schemas.py).
_spec = importlib.util.spec_from_file_location(
    "dubles_http_publico", Path(__file__).resolve().parents[1] / "unit/test_http_publico.py"
)
assert _spec and _spec.loader
_dubles = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _dubles  # @dataclass resolve anotações pelo módulo
_spec.loader.exec_module(_dubles)
URL: str = _dubles.URL
LimitesFalso = _dubles.LimitesFalso
novo_cliente: Callable[..., Any] = _dubles._cliente
corpo: Callable[..., bytes] = _dubles._corpo
cabecalhos: Callable[..., dict[str, str]] = _dubles._cabecalhos

# Formatos que o jsonschema só confere com pacotes opcionais (ausentes aqui): conferidos à mão.
FORMATOS = FormatChecker()


@FORMATOS.checks("date-time", raises=ValueError)
def _data_hora(valor: object) -> bool:
    """RFC 3339: data, `T`, hora e fuso obrigatórios."""
    if not isinstance(valor, str):
        return True
    return "T" in valor and datetime.fromisoformat(valor).tzinfo is not None


@FORMATOS.checks("uri")
def _uri(valor: object) -> bool:
    if not isinstance(valor, str):
        return True
    partes = urlsplit(valor)
    return bool(partes.scheme and partes.netloc)


def _no(ref: str) -> Any:
    no: Any = CONTRATO
    for parte in ref.removeprefix("#/").split("/"):
        no = no[parte]
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

    corpo_json = json.loads(resposta.content)
    validador = Draft4Validator(
        {**schema, "components": COMPONENTES}, format_checker=FORMATOS
    )  # `$ref: #/components/...` resolve na raiz; em Draft 4 o irmão `components` não valida nada
    erros = [f"{list(e.absolute_path)}: {e.message}" for e in validador.iter_errors(corpo_json)]
    assert not erros, f"corpo {status} fora do schema: {erros}"
    nao_declarados = set(corpo_json) - _propriedades(schema)
    assert not nao_declarados, (
        f"membros do corpo {status} não declarados no contrato: {nao_declarados}"
    )
    if media_type == "application/problem+json":
        slug, status_no_catalogo = _linha_do_catalogo(corpo_json["codigo"])
        assert corpo_json["type"].endswith(f"/problemas/{slug}")
        assert corpo_json["status"] == status_no_catalogo == resposta.status_code
    return dict(corpo_json)


@pytest.fixture
def limites() -> Any:
    return LimitesFalso()


@pytest.fixture
def cliente(limites: Any) -> Any:
    return novo_cliente(limites)


def test_201_e_replay_200(cliente: Any) -> None:
    chave = str(uuid.uuid4())
    primeira = cliente.post(URL, content=corpo(), headers=cabecalhos(chave))
    replay = cliente.post(URL, content=corpo(), headers=cabecalhos(chave))

    assert primeira.status_code == 201
    assert replay.status_code == 200
    conferir(primeira, POST)
    conferir(replay, POST)


def test_captura_e_estorno_201_com_id_autorizacao_original(cliente: Any) -> None:
    reserva = cliente.post(URL, content=corpo(), headers=cabecalhos()).json()
    original = reserva["id_autorizacao"]

    captura = cliente.post(
        URL,
        content=corpo("100.00", tipo_operacao="DEBITO", id_autorizacao_original=original),
        headers=cabecalhos(),
    )

    assert captura.status_code == 201
    assert conferir(captura, POST)["id_autorizacao_original"] == original


def test_402_limite_insuficiente_com_extensoes_declaradas(cliente: Any) -> None:
    resposta = cliente.post(URL, content=corpo("1000.01"), headers=cabecalhos())

    assert resposta.status_code == 402
    problema = conferir(resposta, POST)
    assert {"id_autorizacao", "limite_disponivel"} <= set(problema)


def test_409_em_andamento_e_503_dependencia_fora(cliente: Any, limites: Any) -> None:
    chave = str(uuid.uuid4())
    limites.indisponivel = True

    fora = cliente.post(URL, content=corpo(), headers=cabecalhos(chave))
    em_andamento = cliente.post(URL, content=corpo(), headers=cabecalhos(chave))

    assert fora.status_code == 503
    assert fora.headers["retry-after"]
    assert conferir(fora, POST)["codigo"] == "SERVICO_INDISPONIVEL"
    assert em_andamento.status_code == 409
    assert em_andamento.headers["retry-after"]
    assert conferir(em_andamento, POST)["codigo"] == "REQUISICAO_EM_ANDAMENTO"


def test_409_conflito_de_estado(cliente: Any) -> None:
    original = cliente.post(URL, content=corpo(), headers=cabecalhos()).json()
    estorno = corpo(tipo_operacao="ESTORNO", id_autorizacao_original=original["id_autorizacao"])
    cliente.post(URL, content=estorno, headers=cabecalhos())

    segundo = cliente.post(URL, content=estorno, headers=cabecalhos())

    assert segundo.status_code == 409
    assert conferir(segundo, POST)["codigo"] == "CONFLITO_DE_ESTADO"


def test_503_load_shedding(limites: Any) -> None:
    resposta = novo_cliente(limites, max_em_voo=0).post(URL, content=corpo(), headers=cabecalhos())

    assert resposta.status_code == 503
    assert conferir(resposta, POST)["codigo"] == "SERVICO_INDISPONIVEL"


@pytest.mark.parametrize(
    ("caso", "conteudo"),
    [
        ("valor zero", corpo("0")),
        ("valor com 3 casas", corpo("10.001")),
        ("tipo errado", corpo('"10.00"')),
        ("campo desconhecido", corpo(extra="x")),
        ("metadata fora da allowlist", corpo(metadata={"rastreio": "x"})),
        ("id_conta ausente", b'{"valor": 10.00, "moeda": "BRL", "tipo_operacao": "RESERVA"}'),
        ("JSON que não é objeto", b"[]"),
        ("moeda diferente de BRL", corpo(moeda="USD")),
    ],
)
def test_422_corpo_fora_do_schema_ou_da_regra_e_do_servico(
    cliente: Any, caso: str, conteudo: bytes
) -> None:
    """O serviço é a autoridade do corpo (o gateway não o valida): tudo isto é 422, nunca 400."""
    resposta = cliente.post(URL, content=conteudo, headers=cabecalhos())

    assert resposta.status_code == 422, caso
    problema = conferir(resposta, POST)
    assert problema["codigo"] == "VALIDACAO_DE_DOMINIO"
    assert problema["erros"]


def test_422_chave_reutilizada_e_original_invalida(cliente: Any) -> None:
    chave = str(uuid.uuid4())
    cliente.post(URL, content=corpo(), headers=cabecalhos(chave))

    reutilizada = cliente.post(URL, content=corpo("151.00"), headers=cabecalhos(chave))
    original_invalida = cliente.post(
        URL,
        content=corpo(tipo_operacao="ESTORNO", id_autorizacao_original=str(uuid.uuid4())),
        headers=cabecalhos(),
    )

    assert conferir(reutilizada, POST)["codigo"] == "IDEMPOTENCY_KEY_REUTILIZADA"
    assert conferir(original_invalida, POST)["codigo"] == "AUTORIZACAO_ORIGINAL_INVALIDA"


def test_400_idempotency_key_ausente_e_json_malformado(cliente: Any) -> None:
    sem_chave = cabecalhos()
    del sem_chave["Idempotency-Key"]

    ausente = cliente.post(URL, content=corpo(), headers=sem_chave)
    malformado = cliente.post(URL, content=b'{"valor": ', headers=cabecalhos())

    assert ausente.status_code == malformado.status_code == 400
    assert conferir(ausente, POST)["codigo"] == "IDEMPOTENCY_KEY_INVALIDA"
    assert conferir(malformado, POST)["codigo"] == "REQUISICAO_MAL_FORMADA"


def test_401_404_415_e_500(cliente: Any, limites: Any) -> None:
    sem_client_id = cabecalhos()
    del sem_client_id["X-Client-Id"]

    assert conferir(cliente.post(URL, content=corpo(), headers=sem_client_id), POST)["codigo"] == (
        "NAO_AUTENTICADO"
    )
    contrato_inexistente = cliente.post(
        "/v1/contratos/CTR-NAO/autorizacoes", content=corpo(), headers=cabecalhos()
    )
    assert conferir(contrato_inexistente, POST)["codigo"] == "CONTRATO_NAO_ENCONTRADO"
    texto = cliente.post(URL, content=corpo(), headers=cabecalhos(**{"Content-Type": "text/plain"}))
    assert conferir(texto, POST)["codigo"] == "TIPO_DE_MIDIA_NAO_SUPORTADO"

    def explode(*_: object) -> Any:
        raise RuntimeError("falha")

    limites.aplicar = explode
    assert conferir(cliente.post(URL, content=corpo(), headers=cabecalhos()), POST)["codigo"] == (
        "ERRO_INTERNO"
    )


def test_get_200_e_404(cliente: Any) -> None:
    criada = cliente.post(URL, content=corpo(), headers=cabecalhos()).json()
    canal = {"X-Client-Id": "canal-pos"}

    encontrada = cliente.get(f"{URL}/{criada['id_autorizacao']}", headers=canal)
    inexistente = cliente.get(f"{URL}/{uuid.uuid4()}", headers=canal)

    assert encontrada.status_code == 200
    assert conferir(encontrada, GET)["status"] == "APROVADA"
    assert conferir(inexistente, GET)["codigo"] == "AUTORIZACAO_NAO_ENCONTRADA"


def test_o_conferidor_reprova_corpo_fora_do_contrato() -> None:
    """Sanidade do próprio teste: um 201 sem campo obrigatório e com membro extra não passa."""
    ruim = httpx.Response(
        201,
        headers={"content-type": "application/json"},
        content=json.dumps({"id_autorizacao": "nao-e-uuid", "extra": 1}).encode(),
    )

    with pytest.raises(AssertionError, match="fora do schema"):
        conferir(ruim, POST)


# --- painel 6 -------------------------------------------------------------------------------------


def test_debito_puro_e_credito_depois_do_debito_201(cliente: Any) -> None:
    """Os 4 `tipo_operacao` passam pelo conferidor (o dublê agora tem as pré-condições do limite real)."""
    debito = cliente.post(
        URL, content=corpo("300.00", tipo_operacao="DEBITO"), headers=cabecalhos()
    )
    credito = cliente.post(
        URL, content=corpo("100.00", tipo_operacao="CREDITO"), headers=cabecalhos()
    )

    assert debito.status_code == credito.status_code == 201
    assert conferir(debito, POST)["limite_utilizado"] == 300
    pago = conferir(credito, POST)
    assert (pago["limite_utilizado"], pago["saldo_reservado"]) == (200, 0)


def test_422_estorno_sem_utilizado_e_conta_divergente(cliente: Any) -> None:
    debito = cliente.post(URL, content=corpo("50.00", tipo_operacao="DEBITO"), headers=cabecalhos())
    cliente.post(URL, content=corpo("50.00", tipo_operacao="CREDITO"), headers=cabecalhos())
    estorno = cliente.post(
        URL,
        content=corpo(
            "50.00",
            tipo_operacao="ESTORNO",
            id_autorizacao_original=debito.json()["id_autorizacao"],
        ),
        headers=cabecalhos(),
    )
    conta = cliente.post(URL, content=corpo(id_conta="9999-9"), headers=cabecalhos())

    assert conferir(estorno, POST)["codigo"] == "CREDITO_EXCEDE_UTILIZADO"
    assert conferir(conta, POST)["codigo"] == "CONTA_DIVERGENTE"


def test_get_422_id_que_nao_e_uuid(cliente: Any) -> None:
    resposta = cliente.get(f"{URL}/{'a' * 2100}", headers={"X-Client-Id": "canal-pos"})

    assert resposta.status_code == 422
    assert conferir(resposta, GET)["codigo"] == "VALIDACAO_DE_DOMINIO"


def test_405_nas_duas_operacoes(cliente: Any) -> None:
    no_post = cliente.delete(URL, headers=cabecalhos())
    no_get = cliente.post(f"{URL}/{uuid.uuid4()}", content=corpo(), headers=cabecalhos())

    assert no_post.status_code == no_get.status_code == 405
    assert conferir(no_post, POST)["codigo"] == "METODO_NAO_PERMITIDO"
    assert conferir(no_get, GET)["codigo"] == "METODO_NAO_PERMITIDO"
    assert no_get.headers["allow"] == "GET"


def test_404_de_rota_inexistente_e_problema_do_catalogo(cliente: Any) -> None:
    resposta = cliente.post("/v1/nada", content=corpo(), headers=cabecalhos())

    problema = resposta.json()
    assert resposta.status_code == 404
    assert resposta.headers["content-type"] == "application/problem+json"
    assert _linha_do_catalogo(problema["codigo"]) == ("recurso-nao-encontrado", 404)


def test_503_banco_de_autorizacoes_indisponivel(limites: Any) -> None:
    repositorio = RepositorioDeAutorizacoesEmMemoria()

    def estrangulado(*_: object) -> None:
        raise ArmazenamentoIndisponivel("registrar")

    repositorio.registrar = estrangulado  # type: ignore[method-assign,assignment]
    repositorio.obter = estrangulado  # type: ignore[method-assign,assignment]
    cliente = novo_cliente(limites, repositorio=repositorio)

    post = cliente.post(URL, content=corpo(), headers=cabecalhos())
    get = cliente.get(f"{URL}/{uuid.uuid4()}", headers={"X-Client-Id": "canal-pos"})

    assert post.headers["retry-after"] == get.headers["retry-after"] == "1"
    assert conferir(post, POST)["codigo"] == "SERVICO_INDISPONIVEL"
    assert conferir(get, GET)["codigo"] == "SERVICO_INDISPONIVEL"


def test_400_nan_401_client_id_invalido_e_422_com_erros_cortados(cliente: Any) -> None:
    nan = cliente.post(URL, content=corpo("NaN"), headers=cabecalhos())
    intruso = cliente.post(URL, content=corpo(), headers=cabecalhos(**{"X-Client-Id": "a b"}))
    muitos = cliente.post(
        URL, content=corpo(**{f"campo_{i}": i for i in range(30)}), headers=cabecalhos()
    )

    assert conferir(nan, POST)["codigo"] == "REQUISICAO_MAL_FORMADA"
    assert conferir(intruso, POST)["codigo"] == "NAO_AUTENTICADO"
    cortado = conferir(muitos, POST)
    assert (len(cortado["erros"]), cortado["erros_omitidos"]) == (20, 10)


def test_todo_problema_do_servico_esta_no_catalogo_com_o_mesmo_tipo_e_status() -> None:
    divergentes = {
        codigo: (slug, status)
        for codigo, (slug, _title, status) in _PROBLEMAS.items()
        if _linha_do_catalogo(codigo) != (slug, status)
    }
    assert divergentes == {}


def test_motivos_de_recusa_do_contrato_sao_os_do_dominio() -> None:
    declarados = CONTRATO["components"]["schemas"]["Autorizacao"]["properties"]["motivo_recusa"]
    assert set(declarados["enum"]) == {m.value for m in MotivoRecusa}
