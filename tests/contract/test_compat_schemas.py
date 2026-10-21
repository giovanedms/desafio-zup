"""Testes do gate de compatibilidade de schemas (scripts/compat_schemas.py)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("compat_schemas", RAIZ / "scripts/compat_schemas.py")
assert _spec and _spec.loader
compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat)

NOME = "evento-x.v1.schema.json"

BASE: dict[str, Any] = {
    "type": "object",
    "required": ["type", "data"],
    "properties": {
        "type": {"type": "string", "enum": ["a.b.EventoX.v1"]},
        "source": {"type": "string", "enum": ["/financas/x"]},
        "dataschema": {"type": "string", "enum": ["https://e/x/1.0.json"]},
        "data": {
            "type": "object",
            "additionalProperties": True,
            "required": ["valor"],
            "properties": {
                "valor": {"type": "string"},
                "status": {"type": "string", "enum": ["A", "B"]},
                "codigo": {
                    "type": "string",
                    "pattern": "^[A-Z]+$",
                    "minLength": 2,
                    "maxLength": 10,
                },
                "quantia": {"type": "number", "minimum": 0, "maximum": 1000, "multipleOf": 0.01},
                "taxa": {"type": "number"},
            },
        },
    },
}


def _gravar(diretorio: Path, schema: dict[str, Any], nome: str = NOME) -> Path:
    diretorio.mkdir(exist_ok=True)
    (diretorio / nome).write_text(json.dumps(schema), encoding="utf-8")
    return diretorio


def _rodar(tmp_path: Path, novo: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    base_dir = _gravar(tmp_path / "base", BASE)
    novo_dir = tmp_path / "novo"
    novo_dir.mkdir()
    if novo is not None:
        _gravar(novo_dir, novo)
    return compat.comparar_diretorios(base_dir, novo_dir)


def _mutar(fn: Any) -> dict[str, Any]:
    schema = copy.deepcopy(BASE)
    fn(schema)
    return schema


def test_identico_e_campo_opcional_novo_sao_verdes(tmp_path: Path) -> None:
    novo = _mutar(
        lambda s: s["properties"]["data"]["properties"].update({"extra": {"type": "string"}})
    )
    assert _rodar(tmp_path, novo) == ([], [])


def test_campo_novo_required_em_data_quebra(tmp_path: Path) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"]["data"]["properties"]["extra"] = {"type": "string"}
        s["properties"]["data"]["required"].append("extra")

    quebras, _ = _rodar(tmp_path, _mutar(mut))
    assert any("data.extra" in q and "required" in q for q in quebras)


def test_campo_novo_required_na_raiz_quebra(tmp_path: Path) -> None:
    quebras, _ = _rodar(tmp_path, _mutar(lambda s: s["required"].append("traceparent")))
    assert any("traceparent" in q and "required" in q for q in quebras)


def test_propriedade_removida_quebra(tmp_path: Path) -> None:
    quebras, _ = _rodar(
        tmp_path, _mutar(lambda s: s["properties"]["data"]["properties"].pop("status"))
    )
    assert any("data.status" in q and "removida" in q for q in quebras)


def test_type_alterado_quebra(tmp_path: Path) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"]["data"]["properties"]["valor"]["type"] = "number"

    quebras, _ = _rodar(tmp_path, _mutar(mut))
    assert any("data.valor" in q and "type alterado" in q for q in quebras)


def test_valor_removido_de_enum_quebra(tmp_path: Path) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"]["data"]["properties"]["status"]["enum"] = ["A"]

    quebras, _ = _rodar(tmp_path, _mutar(mut))
    assert any("data.status" in q and '"B"' in q for q in quebras)


def test_valor_adicionado_a_enum_so_avisa(tmp_path: Path) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"]["data"]["properties"]["status"]["enum"] = ["A", "B", "C"]

    quebras, avisos = _rodar(tmp_path, _mutar(mut))
    assert quebras == []
    assert any("data.status" in a and '"C"' in a for a in avisos)


@pytest.mark.parametrize("atributo", ["type", "source", "dataschema"])
def test_valor_fixo_de_atributo_ce_alterado_quebra(tmp_path: Path, atributo: str) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"][atributo]["enum"] = ["outro.valor"]

    quebras, _ = _rodar(tmp_path, _mutar(mut))
    assert any(f".{atributo}" in q and "fixo" in q for q in quebras)


def test_adicionar_valor_a_atributo_fixo_tambem_quebra(tmp_path: Path) -> None:
    def mut(s: dict[str, Any]) -> None:
        s["properties"]["type"]["enum"].append("a.b.EventoX.v2")

    quebras, _ = _rodar(tmp_path, _mutar(mut))
    assert any(".type" in q and "fixo" in q for q in quebras)


def _restringir(no: str, **restricoes: Any) -> dict[str, Any]:
    """Cópia da BASE com restrições trocadas num nó: "$" (raiz), "data" ou um campo de `data`.
    Valor None remove a restrição."""
    schema = copy.deepcopy(BASE)
    data = schema["properties"]["data"]
    alvo = {"$": schema, "data": data}.get(no) or data["properties"][no]
    for chave, valor in restricoes.items():
        if valor is None:
            alvo.pop(chave, None)
        else:
            alvo[chave] = valor
    return schema


# Uma classe de aperto por caso: o schema novo recusaria um evento que a base aceitava (BACKWARD).
APERTOS = [
    pytest.param(
        "data", {"additionalProperties": False}, "$.data: additionalProperties", id="ap-true-false"
    ),
    pytest.param(
        "$", {"additionalProperties": False}, "$: additionalProperties", id="ap-ausente-false"
    ),
    pytest.param(
        "data",
        {"additionalProperties": {"type": "string"}},
        "$.data: additionalProperties",
        id="ap-schema",
    ),
    pytest.param("valor", {"pattern": "^[0-9]+$"}, ".valor: pattern novo", id="pattern-novo"),
    pytest.param(
        "codigo",
        {"pattern": "^[A-Z]{3,}$"},
        ".codigo: pattern novo ou alterado",
        id="pattern-alterado",
    ),
    pytest.param("valor", {"format": "date-time"}, ".valor: format novo", id="format-novo"),
    pytest.param("quantia", {"minimum": 10}, ".quantia: minimum", id="minimum-subiu"),
    pytest.param("taxa", {"minimum": 0}, ".taxa: minimum", id="minimum-novo"),
    pytest.param(
        "quantia", {"exclusiveMinimum": True}, ".quantia: minimum", id="exclusiveMinimum-draft04"
    ),
    pytest.param(
        "quantia",
        {"minimum": None, "exclusiveMinimum": 0},
        ".quantia: minimum",
        id="exclusiveMinimum-num",
    ),
    pytest.param("quantia", {"maximum": 100}, ".quantia: maximum", id="maximum-desceu"),
    pytest.param("taxa", {"maximum": 5}, ".taxa: maximum", id="maximum-novo"),
    pytest.param(
        "quantia", {"exclusiveMaximum": True}, ".quantia: maximum", id="exclusiveMaximum-draft04"
    ),
    pytest.param(
        "quantia",
        {"maximum": None, "exclusiveMaximum": 500},
        ".quantia: maximum",
        id="exclusiveMaximum-num",
    ),
    pytest.param("taxa", {"multipleOf": 0.01}, ".taxa: multipleOf", id="multipleOf-novo"),
    pytest.param("quantia", {"multipleOf": 0.1}, ".quantia: multipleOf", id="multipleOf-apertado"),
    pytest.param("codigo", {"maxLength": 5}, ".codigo: maxLength", id="maxLength-desceu"),
    pytest.param("valor", {"maxLength": 36}, ".valor: maxLength", id="maxLength-novo"),
    pytest.param("codigo", {"minLength": 3}, ".codigo: minLength", id="minLength-subiu"),
    # B-2 (painel 6): enum/const criado onde não havia fecha o domínio do campo.
    pytest.param("valor", {"enum": ["CTR-1", "CTR-2"]}, ".valor: enum/const novo", id="enum-novo"),
    pytest.param("valor", {"const": "BRL"}, ".valor: enum/const novo", id="const-novo"),
    pytest.param("taxa", {"enum": [1, 2]}, ".taxa: enum/const novo", id="enum-novo-numero"),
    pytest.param(
        "data",
        {"allOf": [{"required": ["taxa"]}]},
        "$.data: allOf novo ou alterado",
        id="allOf-novo",
    ),
    pytest.param(
        "valor", {"anyOf": [{"maxLength": 3}]}, ".valor: anyOf novo ou alterado", id="anyOf-novo"
    ),
    pytest.param(
        "valor", {"oneOf": [{"maxLength": 3}]}, ".valor: oneOf novo ou alterado", id="oneOf-novo"
    ),
    pytest.param("valor", {"not": {"enum": ["x"]}}, ".valor: not novo ou alterado", id="not-novo"),
    pytest.param(
        "data",
        {"dependencies": {"taxa": ["quantia"]}},
        "$.data: dependencies novo ou alterado",
        id="dependencies-novo",
    ),
    pytest.param(
        "data",
        {"patternProperties": {"^x_": {"type": "string"}}},
        "$.data: patternProperties novo ou alterado",
        id="patternProperties-novo",
    ),
    pytest.param(
        "valor", {"$ref": "#/definitions/codigo"}, ".valor: $ref novo ou alterado", id="ref-novo"
    ),
    pytest.param("data", {"maxProperties": 3}, "$.data: maxProperties", id="maxProperties-novo"),
    pytest.param("data", {"minProperties": 2}, "$.data: minProperties", id="minProperties-novo"),
]

AFROUXAMENTOS = [
    pytest.param("quantia", {"minimum": -1, "maximum": 2000}, id="limites-afrouxados"),
    pytest.param("quantia", {"minimum": None, "maximum": None}, id="limites-removidos"),
    pytest.param("quantia", {"multipleOf": 0.001}, id="multipleOf-mais-fino"),
    pytest.param("quantia", {"multipleOf": None}, id="multipleOf-removido"),
    pytest.param(
        "codigo", {"pattern": None, "minLength": 1, "maxLength": 20}, id="texto-afrouxado"
    ),
    pytest.param("data", {"additionalProperties": None}, id="ap-true-ausente"),
]


@pytest.mark.parametrize(("no", "restricoes", "esperado"), APERTOS)
def test_aperto_de_restricao_quebra(
    tmp_path: Path, no: str, restricoes: dict[str, Any], esperado: str
) -> None:
    quebras, _ = _rodar(tmp_path, _restringir(no, **restricoes))
    assert any(esperado in q for q in quebras), quebras


@pytest.mark.parametrize(("no", "restricoes"), AFROUXAMENTOS)
def test_afrouxar_restricao_e_verde(tmp_path: Path, no: str, restricoes: dict[str, Any]) -> None:
    assert _rodar(tmp_path, _restringir(no, **restricoes)) == ([], [])


def test_arquivo_removido_quebra(tmp_path: Path) -> None:
    quebras, _ = _rodar(tmp_path, None)
    assert any(NOME in q and "removido" in q for q in quebras)


def test_arquivo_novo_e_novo_major_sao_ok(tmp_path: Path) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    novo_dir = _gravar(tmp_path / "novo", BASE)
    _gravar(novo_dir, {"type": "object"}, "evento-y.v1.schema.json")
    _gravar(novo_dir, {"type": "object", "required": ["z"]}, "evento-x.v2.schema.json")
    assert compat.comparar_diretorios(base_dir, novo_dir) == ([], [])


def test_main_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    ruim = _gravar(tmp_path / "ruim", _mutar(lambda s: s["required"].append("x")))
    assert compat.main(["--base", str(base_dir), "--novo", str(base_dir)]) == 0
    assert compat.main(["--base", str(base_dir), "--novo", str(ruim)]) == 1
    assert "QUEBRA" in capsys.readouterr().out


def test_schemas_reais_sao_compativeis_consigo_mesmos() -> None:
    d = RAIZ / "contracts/events"
    assert compat.comparar_diretorios(d, d) == ([], [])


def test_remover_combinador_afrouxa_e_e_verde(tmp_path: Path) -> None:
    com_allof = copy.deepcopy(BASE)
    com_allof["properties"]["data"]["allOf"] = [{"required": ["taxa"]}]
    base_dir = _gravar(tmp_path / "base", com_allof)
    novo_dir = _gravar(tmp_path / "novo", BASE)
    assert compat.comparar_diretorios(base_dir, novo_dir) == ([], [])


@pytest.mark.parametrize(
    ("depois", "esperado"),
    [
        pytest.param({"items": {"type": "string"}}, "[]: type alterado", id="items-novo"),
        pytest.param({"minItems": 1}, ".tags: minItems", id="minItems-novo"),
        pytest.param({"maxItems": 5}, ".tags: maxItems", id="maxItems-novo"),
        pytest.param({"uniqueItems": True}, ".tags: uniqueItems", id="uniqueItems-novo"),
    ],
)
def test_aperto_em_lista_quebra(tmp_path: Path, depois: dict[str, Any], esperado: str) -> None:
    base = copy.deepcopy(BASE)
    base["properties"]["data"]["properties"]["tags"] = {"type": "array"}
    novo = copy.deepcopy(base)
    novo["properties"]["data"]["properties"]["tags"].update(depois)
    base_dir = _gravar(tmp_path / "base", base)
    novo_dir = _gravar(tmp_path / "novo", novo)
    quebras, _ = compat.comparar_diretorios(base_dir, novo_dir)
    assert any(esperado in q for q in quebras), quebras


# ---------------------------------------------------------------- quebras aceitas (registro explícito)

ACEITA_EXTRA = {
    "schema": NOME,
    "caminho": "$.data.extra",
    "regra": "campo novo em required",
    "motivo": "antes de qualquer release",
    "data": "2026-10-21",
}


def _required_extra(s: dict[str, Any]) -> None:
    s["properties"]["data"]["properties"]["extra"] = {"type": "string"}
    s["properties"]["data"]["required"].append("extra")


def _registro(tmp_path: Path, *entradas: dict[str, str]) -> Path:
    arquivo = tmp_path / "quebras-aceitas.json"
    arquivo.write_text(json.dumps({"quebras": list(entradas)}), encoding="utf-8")
    return arquivo


def _main(base: Path, novo: Path, aceitas: Path | None = None) -> int:
    args = ["--base", str(base), "--novo", str(novo)]
    return int(compat.main([*args, "--aceitas", str(aceitas)] if aceitas else args))


def test_quebra_registrada_passa_e_aparece_como_aceita(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    novo_dir = _gravar(tmp_path / "novo", _mutar(_required_extra))

    saida = _main(base_dir, novo_dir, _registro(tmp_path, ACEITA_EXTRA))

    texto = capsys.readouterr().out
    assert saida == 0
    assert f"ACEITA {NOME}:$.data.extra: campo novo em required" in texto
    assert "antes de qualquer release" in texto  # o motivo vai para o log da esteira
    assert "QUEBRA" not in texto


def test_so_a_quebra_exata_e_aceita(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A mesma regra noutro campo continua reprovando."""

    def mut(s: dict[str, Any]) -> None:
        _required_extra(s)
        s["properties"]["data"]["required"].append("taxa")  # outra quebra, não registrada

    base_dir = _gravar(tmp_path / "base", BASE)
    novo_dir = _gravar(tmp_path / "novo", _mutar(mut))

    saida = _main(base_dir, novo_dir, _registro(tmp_path, ACEITA_EXTRA))

    texto = capsys.readouterr().out
    assert saida == 1
    assert f"QUEBRA {NOME}:$.data.taxa: campo novo em required" in texto
    assert texto.count("QUEBRA ") == 1
    assert texto.count("ACEITA ") == 1


def test_sem_o_registro_a_mesma_mudanca_reprova(tmp_path: Path) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    novo_dir = _gravar(tmp_path / "novo", _mutar(_required_extra))
    assert _main(base_dir, novo_dir) == 1


def test_entrada_sem_efeito_so_avisa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)

    saida = _main(base_dir, base_dir, _registro(tmp_path, ACEITA_EXTRA))

    assert saida == 0
    assert "sem efeito" in capsys.readouterr().out


def _sem(chave: str) -> dict[str, str]:
    return {k: v for k, v in ACEITA_EXTRA.items() if k != chave}


@pytest.mark.parametrize(
    "conteudo",
    [
        pytest.param("[]", id="nao-e-objeto"),
        pytest.param('{"quebras": {}}', id="quebras-nao-e-lista"),
        pytest.param("{", id="json-invalido"),
        pytest.param({**ACEITA_EXTRA, "motivo": " "}, id="motivo-vazio"),
        pytest.param(_sem("data"), id="sem-data"),
        pytest.param({**ACEITA_EXTRA, "quem": "x"}, id="campo-extra"),
        pytest.param({**ACEITA_EXTRA, "data": "21/10/2026"}, id="data-fora-do-iso"),
        pytest.param({**ACEITA_EXTRA, "schema": "evento-x.json"}, id="schema-sem-sufixo"),
        pytest.param({**ACEITA_EXTRA, "caminho": "data.x"}, id="caminho-sem-raiz"),
    ],
)
def test_registro_invalido_reprova_fail_closed(
    tmp_path: Path, conteudo: str | dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    aceitas = tmp_path / "quebras-aceitas.json"
    texto = conteudo if isinstance(conteudo, str) else json.dumps({"quebras": [conteudo]})
    aceitas.write_text(texto, encoding="utf-8")

    assert _main(base_dir, base_dir, aceitas) == 2
    assert "ERRO" in capsys.readouterr().out


def test_registro_ausente_reprova(tmp_path: Path) -> None:
    base_dir = _gravar(tmp_path / "base", BASE)
    assert _main(base_dir, base_dir, tmp_path / "nao-existe.json") == 2


# ---------------------------------------------------------------- os contratos reais

EVENTOS = RAIZ / "contracts/events"


def _schema_real(slug: str) -> dict[str, Any]:
    return dict(json.loads((EVENTOS / f"{slug}.v1.schema.json").read_text(encoding="utf-8")))


def test_registro_real_casa_com_a_quebra_que_o_gate_produz(tmp_path: Path) -> None:
    """B-3: a base de antes (tipo_operacao opcional) contra os schemas de hoje dá UMA quebra, a registrada."""
    aceitas = compat.carregar_aceitas(EVENTOS / "quebras-aceitas.json")
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    for arquivo in EVENTOS.glob("*.schema.json"):
        schema = json.loads(arquivo.read_text(encoding="utf-8"))
        if arquivo.name == "transacao-autorizada.v1.schema.json":
            schema["properties"]["data"]["required"].remove("tipo_operacao")
        (base_dir / arquivo.name).write_text(json.dumps(schema), encoding="utf-8")

    quebras, _ = compat.comparar_diretorios(base_dir, EVENTOS)
    restantes, usadas, sem_efeito = compat.separar_aceitas(quebras, aceitas)

    assert quebras == [
        "transacao-autorizada.v1.schema.json:$.data.tipo_operacao: campo novo em required"
    ]
    assert (restantes, len(usadas), sem_efeito) == ([], 1, [])


def test_o_que_a_contabilizacao_exige_e_obrigatorio_no_schema() -> None:
    """B-3: campo que a regra contábil exige para lançar está em `required` (evento válido não vai à DLQ)."""
    exigidos = {
        "transacao-autorizada": {"tipo_operacao", "valor"},
        "transacao-estornada": {"tipo_operacao_original", "valor"},
        "reserva-capturada": {"valor_reservado"},
        "reserva-expirada": {"valor"},
    }
    for slug, campos in exigidos.items():
        schema = _schema_real(slug)
        assert {"id", "source", "time", "data"} <= set(schema["required"]), slug
        exigidos_em_data = {"event_type", "id_contrato", *campos}
        assert exigidos_em_data <= set(schema["properties"]["data"]["required"]), slug


@pytest.mark.parametrize("slug", ["transacao-autorizada", "transacao-recusada"])
def test_canal_e_opcional_com_os_cinco_canais(slug: str) -> None:
    data = _schema_real(slug)["properties"]["data"]
    assert data["properties"]["canal"]["enum"] == ["APP", "WEB", "POS", "PIX", "BACKOFFICE"]
    assert "canal" not in data["required"]


def test_motivo_da_recusa_cobre_todo_motivo_que_o_produtor_publica() -> None:
    """O evento carrega `MotivoRecusa.value` da autorização: valor fora do enum seria evento inválido."""
    from autorizacao.domain.autorizacao import MotivoRecusa  # noqa: PLC0415

    enum = set(
        _schema_real("transacao-recusada")["properties"]["data"]["properties"]["motivo"]["enum"]
    )
    assert {m.value for m in MotivoRecusa} <= enum
    assert "CONTA_DIVERGENTE" in enum  # G4/X2: motivo novo do BC Limites, exposto pela autorização
