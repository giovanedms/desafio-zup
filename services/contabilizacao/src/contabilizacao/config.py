"""Configuração do serviço a partir de variáveis de ambiente (12-Factor)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    tabela_lancamentos: str
    tabela_inbox: str
    regiao: str
    endpoint: str | None  # só no modo local (LocalStack); na AWS fica vazio
    fila: str


def do_ambiente(env: Mapping[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        tabela_lancamentos=e.get("TABELA_LANCAMENTOS", "contabilizacao-lancamentos"),
        tabela_inbox=e.get("TABELA_INBOX", "contabilizacao-inbox"),
        regiao=e.get("AWS_REGION", "sa-east-1"),
        endpoint=e.get("AWS_ENDPOINT_URL") or None,
        fila=e.get("FILA_CONTABILIZACAO", "contabilizacao"),
    )
