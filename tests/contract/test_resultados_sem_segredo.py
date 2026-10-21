"""Os resultados do k6 são versionados como evidência; nenhum pode carregar token ou segredo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RESULTADOS = sorted((Path(__file__).resolve().parents[1] / "load" / "resultados").glob("*.json"))
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
# Também nenhuma chave de acesso AWS (AKIA/ASIA), ARN com conta, URL do API Gateway ou header x-api-key. Hoje o
# resumo não leva URL nem cabeçalho (o handleSummary tira o
# setup_data e a linha com a URL vai só para o stdout): estes padrões travam a regressão se isso mudar. `x-api-key`
# casa o NOME do header, sinal de requisição serializada no resumo; o valor da chave é opaco e não tem padrão.
CHAVE_AWS = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
ARN_COM_CONTA = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:")
URL_DO_GATEWAY = re.compile(r"[a-z0-9]{10}\.execute-api\.[a-z0-9-]+\.amazonaws\.com", re.IGNORECASE)
API_KEY = re.compile(r"x-api-key", re.IGNORECASE)


@pytest.mark.parametrize("arquivo", RESULTADOS, ids=[a.name for a in RESULTADOS])
def test_resultado_de_carga_nao_tem_token(arquivo: Path) -> None:
    texto = arquivo.read_text()

    assert '"setup_data"' not in texto  # o setup() do k6 devolve o token OAuth
    assert not JWT.search(texto)
    for padrao in (CHAVE_AWS, ARN_COM_CONTA, URL_DO_GATEWAY, API_KEY):
        assert not padrao.search(texto), padrao.pattern
