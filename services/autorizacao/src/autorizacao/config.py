"""Configuração do serviço a partir de variáveis de ambiente (12-Factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Configuracao:
    tabela_autorizacoes: str
    tabela_outbox: str
    regiao: str
    endpoint_aws: str | None  # None = endpoint padrão da AWS; LocalStack local aponta para o 4566
    url_limites: str
    max_em_voo: int
    intervalo_reconciliador_s: float = 30.0  # pausa entre rodadas do reconciliador de órfãs

    @classmethod
    def do_ambiente(cls) -> Configuracao:
        """Lê as variáveis do ambiente; variável vazia vale como ausente."""
        return cls(
            tabela_autorizacoes=os.environ.get("TABELA_AUTORIZACOES") or "autorizacoes",
            tabela_outbox=os.environ.get("TABELA_OUTBOX") or "autorizacoes-outbox",
            regiao=os.environ.get("AWS_REGION") or "sa-east-1",
            endpoint_aws=os.environ.get("AWS_ENDPOINT_URL") or None,
            url_limites=os.environ.get("URL_LIMITES") or "http://limites:8080",
            # Dimensionado na AWS: ~capacidade de 1 vCPU (~95 rps) × p99 alvo × 2 (docs/capacidade.md).
            max_em_voo=int(os.environ.get("MAX_EM_VOO") or 32),
            intervalo_reconciliador_s=float(os.environ.get("INTERVALO_RECONCILIADOR_S") or 30),
        )
