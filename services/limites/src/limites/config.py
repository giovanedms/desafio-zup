"""Configuração do serviço a partir de variáveis de ambiente (12-Factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Configuracao:
    tabela_limites: str
    regiao: str
    endpoint_aws: str | None  # None = endpoint padrão da AWS; LocalStack local aponta para o 4566
    fila_legado: str = "legado-contratos"  # fila do feed do core legado (consumida pela ACL)

    @classmethod
    def do_ambiente(cls) -> Configuracao:
        """Lê TABELA_LIMITES, AWS_REGION, AWS_ENDPOINT_URL e FILA_LEGADO; vazia vale como ausente."""
        return cls(
            tabela_limites=os.environ.get("TABELA_LIMITES") or "limites",
            regiao=os.environ.get("AWS_REGION") or "sa-east-1",
            endpoint_aws=os.environ.get("AWS_ENDPOINT_URL") or None,
            fila_legado=os.environ.get("FILA_LEGADO") or "legado-contratos",
        )
