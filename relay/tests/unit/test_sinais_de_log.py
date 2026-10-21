"""Os metric filters dos log groups do relay casam um termo que o código do relay EMITE (B-1, painel 7).

O alarme `cdc-registro-descartado` filtrava `cdc_registro_descartado`, termo que nenhum código emitia desde que o CDC
passou a falhar o lote (`cdc_registro_malformado`): ficou mudo sem nenhum teste ver. Aqui os dois lados se cruzam: o
termo de cada filtro dos log groups do CDC, do relay e do sweeper tem de ser o `evento` de uma linha de log do relay
(os testes de comportamento conferem que a linha sai na condição certa).
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "infra/terraform/modules/observabilidade/main.tf").exists()
)
OBSERVABILIDADE = (RAIZ / "infra/terraform/modules/observabilidade/main.tf").read_text()
LOG_GROUPS_DO_RELAY = {"var.log_group_cdc", "local.log_group_relay", "local.log_group_sweeper"}
# `log_group = <expressão>` seguido de `padrao = "\"<termo>\""` (o filtro por termo, formato do log Text do Lambda).
FILTRO_POR_TERMO = re.compile(r'log_group\s*=\s*(\S+)\s*\n\s*padrao\s*=\s*"\\"(\w+)\\""')


def test_filtros_do_relay_casam_termo_que_o_relay_emite() -> None:
    filtros = [
        (g, t) for g, t in FILTRO_POR_TERMO.findall(OBSERVABILIDADE) if g in LOG_GROUPS_DO_RELAY
    ]
    codigo = "\n".join(p.read_text() for p in (RAIZ / "relay/src/relay").glob("*.py"))

    assert {g for g, _ in filtros} == LOG_GROUPS_DO_RELAY  # todo log group do relay tem filtro
    for _, termo in filtros:
        assert f'"evento": "{termo}"' in codigo, f"nenhuma linha do relay emite {termo!r}"
