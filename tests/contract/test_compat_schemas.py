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
            "required": ["valor"],
            "properties": {
                "valor": {"type": "string"},
                "status": {"type": "string", "enum": ["A", "B"]},
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
