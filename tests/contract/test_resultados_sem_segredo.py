"""Os resultados do k6 são versionados como evidência; nenhum pode carregar token ou segredo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RESULTADOS = sorted((Path(__file__).resolve().parents[1] / "load" / "resultados").glob("*.json"))
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


@pytest.mark.parametrize("arquivo", RESULTADOS, ids=[a.name for a in RESULTADOS])
def test_resultado_de_carga_nao_tem_token(arquivo: Path) -> None:
    texto = arquivo.read_text()

    assert '"setup_data"' not in texto  # o setup() do k6 devolve o token OAuth
    assert not JWT.search(texto)
