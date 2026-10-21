"""Catálogo de schemas: carrega o contrato publicado, valida com o draft do arquivo, confere `date-time` de verdade e
descreve o erro sem o valor recebido."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.nucleo import EventoInvalido
from jsonschema import FormatChecker

CONTRATOS = next(
    p / "contracts" / "events"
    for p in Path(__file__).resolve().parents
    if (p / "contracts" / "events").is_dir()
)
EXEMPLOS = sorted(
    p.name.removesuffix(".v1.json") for p in (CONTRATOS / "examples").glob("*.v1.json")
)


def test_o_catalogo_tem_um_validador_por_schema_publicado(catalogo: CatalogoDeEsquemas) -> None:
    tipos_dos_exemplos = {
        json.loads((CONTRATOS / "examples" / f"{n}.v1.json").read_text())["type"] for n in EXEMPLOS
    }
    assert len(list(CONTRATOS.glob("*.schema.json"))) == len(catalogo.tipos) == 8
    assert catalogo.tipos == tipos_dos_exemplos


@pytest.mark.parametrize("nome", EXEMPLOS)
def test_todo_exemplo_publicado_valida(
    catalogo: CatalogoDeEsquemas, cloudevent: Callable[..., dict[str, Any]], nome: str
) -> None:
    catalogo.validar(cloudevent(nome))


@pytest.mark.parametrize(
    "instante",
    ["2026-10-20T13:45:12.345Z", "2026-10-20T13:45:12Z", "2026-10-20t13:45:12.5-03:00"],
)
def test_date_time_no_formato_rfc3339_passa(
    catalogo: CatalogoDeEsquemas, cloudevent: Callable[..., dict[str, Any]], instante: str
) -> None:
    ce = cloudevent()
    ce["time"] = instante
    catalogo.validar(ce)


@pytest.mark.parametrize(
    "instante",
    [
        "ontem",
        "2026-10-20 13:45:12Z",  # espaço no lugar do T
        "2026-10-20T13:45:12",  # sem fuso
        "2026-10-20",  # só a data (o fromisoformat sozinho aceitaria)
        "2026-13-01T00:00:00Z",  # mês 13
        "2026-02-30T00:00:00Z",  # 30 de fevereiro
    ],
)
def test_date_time_fora_do_rfc3339_e_recusado(
    catalogo: CatalogoDeEsquemas, cloudevent: Callable[..., dict[str, Any]], instante: str
) -> None:
    # Sem o checker próprio, o jsonschema aceitaria todos estes: o formato date-time dele depende de um pacote opcional.
    ce = cloudevent()
    ce["time"] = instante

    with pytest.raises(EventoInvalido) as erro:
        catalogo.validar(ce)

    assert erro.value.erros == ("$.time: format",)


def test_o_checker_de_date_time_nao_vaza_para_o_jsonschema_global() -> None:
    assert "date-time" not in FormatChecker().checkers


def test_erro_diz_caminho_e_regra_sem_o_valor_recebido(
    catalogo: CatalogoDeEsquemas, cloudevent: Callable[..., dict[str, Any]]
) -> None:
    ce = cloudevent(valor=-123.45, moeda="REAL", canal="EMAIL-DO-CLIENTE")
    del ce["data"]["id_autorizacao"]

    with pytest.raises(EventoInvalido) as erro:
        catalogo.validar(ce)

    assert erro.value.erros == (
        "$.data.canal: enum",
        "$.data.moeda: pattern",
        "$.data.valor: minimum",
        "$.data: required id_autorizacao",
    )
    for valor in ("123.45", "REAL", "EMAIL-DO-CLIENTE"):
        assert valor not in str(erro.value)


def test_tipo_sem_schema_no_catalogo_e_recusado(
    catalogo: CatalogoDeEsquemas, cloudevent: Callable[..., dict[str, Any]]
) -> None:
    ce = cloudevent()
    ce["type"] = "example.financas.autorizacao.TransacaoAutorizada.v2"  # outro major: não adivinhar

    with pytest.raises(EventoInvalido) as erro:
        catalogo.validar(ce)

    assert erro.value.erros == ("$.type: sem schema no catálogo",)


def _gravar(diretorio: Path, nome: str, esquema: dict[str, Any]) -> None:
    (diretorio / nome).write_text(json.dumps(esquema), encoding="utf-8")


ESQUEMA_MINIMO = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "properties": {"type": {"type": "string", "enum": ["x.Tipo.v1"]}},
}


def test_diretorio_sem_schema_derruba_a_carga(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CatalogoDeEsquemas.do_diretorio(tmp_path)


def test_schema_sem_draft_conhecido_derruba_a_carga(tmp_path: Path) -> None:
    _gravar(tmp_path, "a.schema.json", {k: v for k, v in ESQUEMA_MINIMO.items() if k != "$schema"})

    with pytest.raises(ValueError, match=r"\$schema"):
        CatalogoDeEsquemas.do_diretorio(tmp_path)


def test_schema_que_nao_fixa_o_type_derruba_a_carga(tmp_path: Path) -> None:
    _gravar(
        tmp_path, "a.schema.json", {**ESQUEMA_MINIMO, "properties": {"type": {"type": "string"}}}
    )

    with pytest.raises(ValueError, match="enum de um valor"):
        CatalogoDeEsquemas.do_diretorio(tmp_path)


def test_dois_schemas_para_o_mesmo_tipo_derrubam_a_carga(tmp_path: Path) -> None:
    _gravar(tmp_path, "a.schema.json", ESQUEMA_MINIMO)
    _gravar(tmp_path, "b.schema.json", ESQUEMA_MINIMO)

    with pytest.raises(ValueError, match="dois schemas"):
        CatalogoDeEsquemas.do_diretorio(tmp_path)
