"""O adapter HTTP não publica a spec gerada pelo FastAPI: o contrato é contracts/openapi/limites-interno-v1.yaml."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from limites.adapters.inbound.http import criar_app
from limites.adapters.outbound.memoria import RepositorioDeLimitesEmMemoria
from limites.application.casos_de_uso import AplicarOperacaoNoLimite, ConsultarLimite


class RelogioFixo:
    def agora(self) -> datetime:
        return datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("caminho", ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"])
def test_spec_gerada_pelo_fastapi_nao_e_publicada(caminho: str) -> None:
    repositorio = RepositorioDeLimitesEmMemoria()
    app = criar_app(
        AplicarOperacaoNoLimite(repositorio, RelogioFixo()), ConsultarLimite(repositorio)
    )

    assert TestClient(app).get(caminho).status_code == 404
