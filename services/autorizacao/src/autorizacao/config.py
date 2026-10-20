"""Configuração do serviço a partir de variáveis de ambiente (12-Factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from autorizacao.adapters.outbound.dynamodb import SHARDS_DE_ORFAS, SHARDS_DE_VENCIMENTOS

# Load shedding: capacidade da task × p99 alvo × 2 (docs/capacidade.md). Medido na AWS (1.0.3): ~8 ms de CPU por
# autorização, ~125 rps por vCPU a 100%. A task entregue tem 0,5 vCPU: ~62 rps × 0,15 s × 2 ≈ 18. Com 1 vCPU
# seriam ~37. O Terraform deriva o valor da CPU da task e passa em MAX_EM_VOO; este default vale para 0,5 vCPU.
MAX_EM_VOO_PADRAO = 18


@dataclass(frozen=True, slots=True)
class Configuracao:
    tabela_autorizacoes: str
    tabela_outbox: str
    regiao: str
    endpoint_aws: str | None  # None = endpoint padrão da AWS; LocalStack local aponta para o 4566
    url_limites: str
    max_em_voo: int
    intervalo_reconciliador_s: float = 30.0  # pausa entre rodadas do reconciliador de órfãs
    # Shards dos GSIs esparsos (conta em adapters/outbound/dynamodb.py). Escritor (autorizacao) e leitor
    # (reconciliador) usam o MESMO valor; subir é seguro, reduzir esconde itens dos shards >= N.
    shards_de_orfas: int = SHARDS_DE_ORFAS
    shards_de_vencimentos: int = SHARDS_DE_VENCIMENTOS

    @classmethod
    def do_ambiente(cls) -> Configuracao:
        """Lê as variáveis do ambiente; variável vazia vale como ausente."""
        return cls(
            tabela_autorizacoes=os.environ.get("TABELA_AUTORIZACOES") or "autorizacoes",
            tabela_outbox=os.environ.get("TABELA_OUTBOX") or "autorizacoes-outbox",
            regiao=os.environ.get("AWS_REGION") or "sa-east-1",
            endpoint_aws=os.environ.get("AWS_ENDPOINT_URL") or None,
            url_limites=os.environ.get("URL_LIMITES") or "http://limites:8080",
            max_em_voo=int(os.environ.get("MAX_EM_VOO") or MAX_EM_VOO_PADRAO),
            intervalo_reconciliador_s=float(os.environ.get("INTERVALO_RECONCILIADOR_S") or 30),
            shards_de_orfas=int(os.environ.get("SHARDS_DE_ORFAS") or SHARDS_DE_ORFAS),
            shards_de_vencimentos=int(
                os.environ.get("SHARDS_DE_VENCIMENTOS") or SHARDS_DE_VENCIMENTOS
            ),
        )
