"""Contexto de correlação por requisição (somente stdlib).

O adapter HTTP de entrada define `correlation_id` e `traceparent` no início da requisição; adapters de
saída (HTTP entre serviços, logs) leem daqui sem que a informação atravesse as assinaturas do domínio.
`ContextVar` é isolado por tarefa asyncio e é copiado para o threadpool pelo `run_in_threadpool`.

`gateway_request_id` é o `$context.requestId` do API Gateway (o mesmo do access log da borda e do
`correlation_id` dos erros que o próprio gateway gera): é a chave de junção entre a linha da borda e a
do serviço. Fora da AWS não existe, e fica None.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

correlation_id_atual: ContextVar[str | None] = ContextVar("correlation_id_atual", default=None)
traceparent_atual: ContextVar[str | None] = ContextVar("traceparent_atual", default=None)
gateway_request_id_atual: ContextVar[str | None] = ContextVar(
    "gateway_request_id_atual", default=None
)

TokensDeContexto = tuple[Token[str | None], Token[str | None], Token[str | None]]


def definir_contexto(
    correlation_id: str | None, traceparent: str | None, gateway_request_id: str | None = None
) -> TokensDeContexto:
    """Fixa o contexto da requisição corrente e devolve os tokens para `limpar_contexto`."""
    return (
        correlation_id_atual.set(correlation_id),
        traceparent_atual.set(traceparent),
        gateway_request_id_atual.set(gateway_request_id),
    )


def limpar_contexto(tokens: TokensDeContexto) -> None:
    """Restaura o estado anterior ao `definir_contexto` que gerou os tokens."""
    correlation_id_atual.reset(tokens[0])
    traceparent_atual.reset(tokens[1])
    gateway_request_id_atual.reset(tokens[2])


def contexto_de_log() -> dict[str, str]:
    """Identificadores da requisição corrente para o `extra=` de um log (só os presentes)."""
    campos = {
        "correlation_id": correlation_id_atual.get(),
        "gateway_request_id": gateway_request_id_atual.get(),
    }
    return {nome: valor for nome, valor in campos.items() if valor is not None}
