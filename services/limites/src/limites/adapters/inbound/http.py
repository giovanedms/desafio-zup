"""Adapter de entrada HTTP (FastAPI) do BC Limites.

Contrato (fonte da verdade): contracts/openapi/limites-interno-v1.yaml.
Problemas: RFC 9457, tipos em contracts/problem-types/catalogo.md.

Dois cuidados com dinheiro, porque float binário não representa 0.1:
  - entrada: o corpo é lido cru e parseado com `parse_float=Decimal`, então o número chega ao
    domínio exatamente como foi escrito no JSON (o body do FastAPI passaria por float);
  - saída: `RespostaJsonExata` serializa Decimal como número JSON, sem float no caminho.

Chamadas ao caso de uso são bloqueantes (boto3): vão para o threadpool, nunca direto no loop.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import simplejson
from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from plataforma.problemas import MEDIA_TYPE, Problema
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from limites.application.casos_de_uso import AplicarOperacaoNoLimite, ConsultarLimite
from limites.application.portas import (
    ArmazenamentoIndisponivel,
    ContencaoPersistente,
    ContratoNaoEncontrado,
    PrazoDoChamadorVencido,
)
from limites.domain.dinheiro import ValorMonetarioInvalido
from limites.domain.limite import Limite
from limites.domain.operacoes import (
    CapturarReserva,
    Creditar,
    Debitar,
    EstornarDebito,
    LiberarReserva,
    OperacaoDeLimite,
    Reservar,
)
from limites.domain.resultados import ResultadoDaOperacao, Saldos

_log = logging.getLogger(__name__)

_DUAS_CASAS = Decimal("0.01")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

# Mesmo padrão de `IdContrato` no contrato OpenAPI: 20 posições é o NUM-CONTRATO do registro do legado
# (ACL, `traducao.py`); aceitar mais seria publicar ids que a réplica nunca terá.
IdContrato = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,20}$")]
# Mesmo padrão do `id_conta` da API pública (16 posições: NUM-CONTA do registro do legado).
IdConta = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,16}$")]

# codigo -> (slug do `type`, title, status HTTP); espelha contracts/problem-types/catalogo.md.
_PROBLEMAS: Mapping[str, tuple[str, str, int]] = {
    "REQUISICAO_MAL_FORMADA": ("requisicao-mal-formada", "Requisição mal formada", 400),
    "CONTRATO_NAO_ENCONTRADO": ("contrato-nao-encontrado", "Contrato não encontrado", 404),
    "VALIDACAO_DE_DOMINIO": ("validacao-de-dominio", "Validação de domínio", 422),
    "ERRO_INTERNO": ("erro-interno", "Erro interno", 500),
    "SERVICO_INDISPONIVEL": ("servico-indisponivel", "Serviço indisponível", 503),
}


class RespostaJsonExata(JSONResponse):
    """JSONResponse que escreve Decimal como número JSON exato (nem float, nem string)."""

    def render(self, content: Any) -> bytes:
        return simplejson.dumps(content, use_decimal=True, ensure_ascii=False).encode("utf-8")


class OperacaoRequest(BaseModel):
    """Corpo de POST .../operacoes (schema `Operacao` do contrato)."""

    model_config = ConfigDict(extra="forbid")

    id_operacao: UUID
    tipo: Literal[
        "RESERVAR", "DEBITAR", "CREDITAR", "LIBERAR_RESERVA", "CAPTURAR_RESERVA", "ESTORNAR_DEBITO"
    ]
    valor: Decimal
    id_reserva: UUID | None = None
    valor_reservado: Decimal | None = None
    id_conta: IdConta | None = None  # se vier, ≠ da conta do contrato → RECUSADA/CONTA_DIVERGENTE


class _Rejeicao(Exception):
    """Requisição recusada na borda; vira um problema (nunca vaza para o framework)."""

    def __init__(
        self, codigo: str, detail: str, extras: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.detail = detail
        self.extras: Mapping[str, object] = extras or {}


def _erro_de_campo(campo: str, mensagem: str) -> dict[str, str]:
    return {"campo": campo, "mensagem": mensagem}


def _ler_operacao(corpo: bytes) -> OperacaoDeLimite:
    """JSON cru -> operação de domínio. `_Rejeicao` 400 (sintaxe) ou 422 (validação)."""
    try:
        bruto = json.loads(corpo, parse_float=Decimal)
    except (
        ValueError,
        RecursionError,
    ) as erro:  # JSONDecodeError e UnicodeDecodeError ⊂ ValueError
        raise _Rejeicao("REQUISICAO_MAL_FORMADA", "O corpo não é um JSON válido.") from erro
    try:
        requisicao = OperacaoRequest.model_validate(bruto)
    except ValidationError as erro:
        erros = [
            _erro_de_campo(".".join(str(parte) for parte in e["loc"]) or "corpo", e["msg"])
            for e in erro.errors(include_url=False, include_context=False, include_input=False)
        ]
        raise _Rejeicao("VALIDACAO_DE_DOMINIO", "Operação inválida.", {"erros": erros}) from erro
    try:
        return _para_operacao(requisicao)
    except ValorMonetarioInvalido as erro:
        raise _Rejeicao(
            "VALIDACAO_DE_DOMINIO",
            "Operação inválida.",
            {"erros": [_erro_de_campo("valor", str(erro))]},
        ) from erro


def _para_operacao(r: OperacaoRequest) -> OperacaoDeLimite:
    """Ids viram `str(uuid)` canônico: mesma operação, mesma chave de idempotência."""
    id_operacao, conta = str(r.id_operacao), r.id_conta
    match r.tipo:
        case "RESERVAR":
            return Reservar(id_operacao, r.valor, id_conta=conta)
        case "DEBITAR":
            return Debitar(id_operacao, r.valor, id_conta=conta)
        case "CREDITAR":
            return Creditar(id_operacao, r.valor, id_conta=conta)
        case "ESTORNAR_DEBITO":
            return EstornarDebito(id_operacao, r.valor, id_conta=conta)
        case "LIBERAR_RESERVA":
            return LiberarReserva(id_operacao, _id_reserva(r), r.valor, id_conta=conta)
        case "CAPTURAR_RESERVA":
            if r.valor_reservado is None:
                raise _campo_obrigatorio("valor_reservado", r.tipo)
            return CapturarReserva(
                id_operacao, _id_reserva(r), r.valor_reservado, r.valor, id_conta=conta
            )


def _id_reserva(r: OperacaoRequest) -> str:
    if r.id_reserva is None:
        raise _campo_obrigatorio("id_reserva", r.tipo)
    return str(r.id_reserva)


def _campo_obrigatorio(campo: str, tipo: str) -> _Rejeicao:
    return _Rejeicao(
        "VALIDACAO_DE_DOMINIO",
        "Operação inválida.",
        {"erros": [_erro_de_campo(campo, f"obrigatório em {tipo}")]},
    )


def _dinheiro_json(valor: Decimal) -> Decimal:
    return valor.quantize(_DUAS_CASAS)


def _saldos_json(saldos: Saldos) -> dict[str, object]:
    return {
        "limite_concedido": _dinheiro_json(saldos.limite_concedido),
        "limite_utilizado": _dinheiro_json(saldos.limite_utilizado),
        "saldo_reservado": _dinheiro_json(saldos.saldo_reservado),
        "limite_disponivel": _dinheiro_json(saldos.limite_disponivel),
        "versao": saldos.versao,
    }


def _resultado_json(resultado: ResultadoDaOperacao) -> dict[str, object]:
    corpo: dict[str, object] = {"resultado": resultado.desfecho.value}
    if resultado.motivo is not None:
        corpo["motivo"] = resultado.motivo.value
    corpo["saldos"] = _saldos_json(resultado.saldos)
    return corpo


def _limite_json(limite: Limite) -> dict[str, object]:
    return {
        "id_contrato": limite.id_contrato,
        "status_contrato": limite.status.value,
        "tipo_produto": limite.tipo_produto.value,
        **_saldos_json(limite.saldos),
        "reservas_ativas": len(limite.reservas),
        "versao_legado": limite.versao_legado,
    }


def _prazo(cabecalho: str | None) -> datetime | None:
    """`X-Prazo-Ms`: epoch em ms até quando o chamador espera. Ausente ou inválido = sem prazo."""
    if not cabecalho or not cabecalho.isdigit():
        return None
    return datetime.fromtimestamp(int(cabecalho) / 1000, UTC)


def _uuid_canonico(valor: str | None) -> str | None:
    """UUID na forma 8-4-4-4-12 (UUID() sozinho aceitaria chaves, `urn:uuid:` e sem hífens)."""
    if valor is None:
        return None
    try:
        analisado = UUID(valor)
    except ValueError:
        return None
    canonico = str(analisado)
    return canonico if canonico == valor.lower() else None


def _trace_id(traceparent: str | None) -> str | None:
    correspondencia = _TRACEPARENT.match(traceparent) if traceparent else None
    return correspondencia["trace_id"] if correspondencia else None


def _problema(
    request: Request,
    codigo: str,
    detail: str | None = None,
    extras: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> RespostaJsonExata:
    slug, title, status = _PROBLEMAS[codigo]
    problema = Problema(
        type_slug=slug,
        title=title,
        status=status,
        codigo=codigo,
        detail=detail,
        correlation_id=request.state.correlation_id,
        trace_id=request.state.trace_id,
        extras=extras or {},
    )
    return RespostaJsonExata(
        problema.para_dict(), status_code=status, headers=headers, media_type=MEDIA_TYPE
    )


def criar_app(aplicar: AplicarOperacaoNoLimite, consultar: ConsultarLimite) -> FastAPI:
    app = FastAPI(
        title="API interna de Limites",
        version="1.0.0",
        openapi_url=None,  # o contrato é contracts/openapi/limites-interno-v1.yaml, não a spec gerada
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def contexto_da_requisicao(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Correlação em TODA resposta, inclusive 404/405 do roteador e o 500 inesperado."""
        request.state.correlation_id = _uuid_canonico(
            request.headers.get("x-correlation-id")
        ) or str(uuid4())
        request.state.trace_id = _trace_id(request.headers.get("traceparent"))
        try:
            resposta = await call_next(request)
        except Exception:
            _log.exception(
                "erro inesperado", extra={"correlation_id": request.state.correlation_id}
            )
            resposta = _problema(
                request, "ERRO_INTERNO", "Falha inesperada. Informe o correlation_id."
            )
        resposta.headers["X-Correlation-Id"] = request.state.correlation_id
        return resposta

    @app.exception_handler(RequestValidationError)
    async def parametro_invalido(request: Request, erro: RequestValidationError) -> Response:
        erros = [
            _erro_de_campo(".".join(str(parte) for parte in e["loc"][1:]) or "requisicao", e["msg"])
            for e in erro.errors()
        ]
        return _problema(request, "VALIDACAO_DE_DOMINIO", "Parâmetro inválido.", {"erros": erros})

    @app.get("/saude")
    async def saude() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/interno/v1/contratos/{id_contrato}/operacoes")
    async def aplicar_operacao(request: Request, id_contrato: IdContrato) -> Response:
        try:
            operacao = _ler_operacao(await request.body())
        except _Rejeicao as rejeicao:
            return _problema(request, rejeicao.codigo, rejeicao.detail, rejeicao.extras)
        prazo = _prazo(request.headers.get("x-prazo-ms"))
        try:
            resultado = await run_in_threadpool(aplicar.executar, id_contrato, operacao, prazo)
        except ContratoNaoEncontrado:
            return _problema(request, "CONTRATO_NAO_ENCONTRADO", "Contrato inexistente.")
        except PrazoDoChamadorVencido:
            return _problema(
                request,
                "SERVICO_INDISPONIVEL",
                "Prazo do chamador vencido antes da escrita; nada foi aplicado.",
                headers={"Retry-After": "1"},
            )
        except (ContencaoPersistente, ArmazenamentoIndisponivel) as erro:
            _log.warning(
                "%s no contrato",
                type(erro).__name__,
                extra={"correlation_id": request.state.correlation_id},
            )
            return _problema(
                request,
                "SERVICO_INDISPONIVEL",
                "Contrato em contenção ou banco sobrecarregado; repita com o mesmo id_operacao.",
                headers={"Retry-After": "1"},
            )
        return RespostaJsonExata(_resultado_json(resultado))

    @app.get("/interno/v1/contratos/{id_contrato}/operacoes/{id_operacao}")
    async def consultar_operacao(
        request: Request, id_contrato: IdContrato, id_operacao: UUID
    ) -> Response:
        """Leitura consistente: a operação está na guarda? o hold com este id está ativo? Usada pelo
        reconciliador para PERGUNTAR em vez de aplicar para descobrir (nada é escrito)."""
        try:
            limite = await run_in_threadpool(consultar.executar, id_contrato)
        except ContratoNaoEncontrado:
            return _problema(request, "CONTRATO_NAO_ENCONTRADO", "Contrato inexistente.")
        except ArmazenamentoIndisponivel:
            return _problema(
                request,
                "SERVICO_INDISPONIVEL",
                "Banco sobrecarregado; tente de novo.",
                headers={"Retry-After": "1"},
            )
        id_op = str(id_operacao)
        return RespostaJsonExata(
            {
                "id_operacao": id_op,
                "na_guarda": limite.ja_aplicou(id_op),
                "reserva_ativa": limite.tem_reserva(id_op),
                "versao": limite.versao,
            }
        )

    @app.get("/interno/v1/contratos/{id_contrato}/limite")
    async def consultar_limite(request: Request, id_contrato: IdContrato) -> Response:
        try:
            limite = await run_in_threadpool(consultar.executar, id_contrato)
        except ContratoNaoEncontrado:
            return _problema(request, "CONTRATO_NAO_ENCONTRADO", "Contrato inexistente.")
        except ArmazenamentoIndisponivel:
            return _problema(
                request,
                "SERVICO_INDISPONIVEL",
                "Banco sobrecarregado; tente de novo.",
                headers={"Retry-After": "1"},
            )
        return RespostaJsonExata(_limite_json(limite))

    return app
