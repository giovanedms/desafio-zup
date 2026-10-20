"""Contexto de correlação por requisição (somente stdlib).

O adapter HTTP de entrada define `correlation_id` e `traceparent` no início da requisição; adapters de
saída (HTTP entre serviços, logs) leem daqui sem que a informação atravesse as assinaturas do domínio.
`ContextVar` é isolado por tarefa asyncio e é copiado para o threadpool pelo `run_in_threadpool`.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

correlation_id_atual: ContextVar[str | None] = ContextVar("correlation_id_atual", default=None)
traceparent_atual: ContextVar[str | None] = ContextVar("traceparent_atual", default=None)

TokensDeContexto = tuple[Token[str | None], Token[str | None]]


def definir_contexto(correlation_id: str | None, traceparent: str | None) -> TokensDeContexto:
    """Fixa o contexto da requisição corrente e devolve os tokens para `limpar_contexto`."""
    return correlation_id_atual.set(correlation_id), traceparent_atual.set(traceparent)


def limpar_contexto(tokens: TokensDeContexto) -> None:
    """Restaura o estado anterior ao `definir_contexto` que gerou os tokens."""
    correlation_id_atual.reset(tokens[0])
    traceparent_atual.reset(tokens[1])
