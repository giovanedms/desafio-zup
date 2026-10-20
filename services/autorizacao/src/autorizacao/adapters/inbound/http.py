"""Adapter de entrada HTTP (FastAPI) do BC Autorização.

Responsabilidade única (SRP): este módulo muda quando muda o contrato HTTP (status, problem+json,
headers, formato do corpo), não quando muda a regra de autorização, o banco ou o formato do evento.

Contrato (fonte da verdade): contracts/openapi/autorizacao-v1.yaml. O FastAPI não publica spec gerada
(`/openapi.json`, `/docs`, `/redoc` desligados): ela divergia do contrato.
Problemas: RFC 9457, tipos em contracts/problem-types/catalogo.md.

Dinheiro sem float: o corpo é lido cru e parseado com `parse_float=Decimal`; a saída usa
`RespostaJsonExata` (Decimal como número JSON). Chamadas ao caso de uso são bloqueantes (boto3/httpx)
e vão para o threadpool.

Replay: o corpo da aprovação é montado SÓ com campos gravados na autorização (inclusive o
correlation_id original), então o replay é idêntico byte a byte ao da primeira resposta.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import simplejson
from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from plataforma.observabilidade import contexto_de_log, definir_contexto, limpar_contexto
from plataforma.problemas import MEDIA_TYPE, Problema
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from autorizacao.application.autorizar import AutorizarTransacao, Contexto, Desfecho
from autorizacao.application.consultar import AutorizacaoNaoEncontrada, ConsultarAutorizacao
from autorizacao.application.portas import (
    AutorizacaoOriginalInvalida,
    ChaveReutilizada,
    ConflitoDeEstado,
    EmAndamento,
    EmReconciliacao,
    EncerradaSemResultado,
    LimitesIndisponivel,
)
from autorizacao.domain.autorizacao import Autorizacao, MotivoRecusa, Resultado
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.idempotencia import EscopoDeIdempotencia
from autorizacao.domain.pedido import PedidoDeAutorizacao, PedidoInvalido, TipoOperacao

_log = logging.getLogger(__name__)

_DUAS_CASAS = Decimal("0.01")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_ID_FORMA_LIVRE = r"^[A-Za-z0-9._:-]+$"
_UUID_REGEX = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# `$context.requestId` que o API Gateway injeta em X-Gateway-Request-Id (junção borda × serviço). Valor
# fora deste formato é ignorado, como o X-Correlation-Id (proteção contra log injection).
_GATEWAY_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

IdContrato = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,20}$")]  # NUM-CONTRATO do legado (ACL)

# codigo -> (slug do `type`, title, status HTTP); espelha contracts/problem-types/catalogo.md.
_PROBLEMAS: Mapping[str, tuple[str, str, int]] = {
    "REQUISICAO_MAL_FORMADA": ("requisicao-mal-formada", "Requisição mal formada", 400),
    "IDEMPOTENCY_KEY_INVALIDA": ("idempotency-key-invalida", "Idempotency-Key inválida", 400),
    "NAO_AUTENTICADO": ("nao-autenticado", "Não autenticado", 401),
    "LIMITE_INSUFICIENTE": ("limite-insuficiente", "Limite insuficiente", 402),
    "CONTRATO_NAO_ENCONTRADO": ("contrato-nao-encontrado", "Contrato não encontrado", 404),
    "AUTORIZACAO_NAO_ENCONTRADA": (
        "autorizacao-nao-encontrada",
        "Autorização não encontrada",
        404,
    ),
    "REQUISICAO_EM_ANDAMENTO": ("requisicao-em-andamento", "Requisição em andamento", 409),
    "CONFLITO_DE_ESTADO": ("conflito-de-estado", "Conflito de estado", 409),
    "TIPO_DE_MIDIA_NAO_SUPORTADO": (
        "tipo-de-midia-nao-suportado",
        "Tipo de mídia não suportado",
        415,
    ),
    "VALIDACAO_DE_DOMINIO": ("validacao-de-dominio", "Validação de domínio", 422),
    "IDEMPOTENCY_KEY_REUTILIZADA": (
        "idempotency-key-reutilizada",
        "Idempotency-Key reutilizada",
        422,
    ),
    "CONTRATO_INATIVO": ("contrato-inativo", "Contrato inativo", 422),
    "CREDITO_EXCEDE_UTILIZADO": ("credito-excede-utilizado", "Crédito excede o utilizado", 422),
    "AUTORIZACAO_ORIGINAL_INVALIDA": (
        "autorizacao-original-invalida",
        "Autorização original inválida",
        422,
    ),
    "ERRO_INTERNO": ("erro-interno", "Erro interno", 500),
    "SERVICO_INDISPONIVEL": ("servico-indisponivel", "Serviço indisponível", 503),
}

# Recusa registrada -> código do problema (402/404/409/422).
_CODIGO_DA_RECUSA: Mapping[MotivoRecusa, str] = {
    MotivoRecusa.LIMITE_INSUFICIENTE: "LIMITE_INSUFICIENTE",
    MotivoRecusa.CONTRATO_INATIVO: "CONTRATO_INATIVO",
    MotivoRecusa.CREDITO_EXCEDE_UTILIZADO: "CREDITO_EXCEDE_UTILIZADO",
    MotivoRecusa.CONTRATO_NAO_ENCONTRADO: "CONTRATO_NAO_ENCONTRADO",
    MotivoRecusa.ORIGINAL_JA_ENCERRADA: "CONFLITO_DE_ESTADO",
    MotivoRecusa.RESERVA_DIVERGENTE: "CONFLITO_DE_ESTADO",
    MotivoRecusa.RESERVA_NAO_ENCONTRADA: "CONFLITO_DE_ESTADO",
    MotivoRecusa.ESTORNO_EM_ANDAMENTO: "CONFLITO_DE_ESTADO",
    MotivoRecusa.RECUSA_DO_LIMITE: "VALIDACAO_DE_DOMINIO",
}

_DETALHE_DA_RECUSA: Mapping[str, str] = {
    "LIMITE_INSUFICIENTE": "O limite disponível do contrato não cobre o valor solicitado.",
    "CONTRATO_INATIVO": "O contrato não está ativo.",
    "CREDITO_EXCEDE_UTILIZADO": "O crédito é maior que o limite utilizado.",
    "CONTRATO_NAO_ENCONTRADO": "Contrato inexistente.",
    "CONFLITO_DE_ESTADO": "A autorização original já foi encerrada.",
}
# Recusas 409 que não são "original encerrada": detalhe próprio, mesmo código.
_DETALHE_DO_MOTIVO: Mapping[MotivoRecusa, str] = {
    MotivoRecusa.RESERVA_NAO_ENCONTRADA: "A reserva já foi capturada, estornada ou expirada por outra operação.",
    MotivoRecusa.ESTORNO_EM_ANDAMENTO: "Outro estorno desta autorização está em andamento.",
    MotivoRecusa.RECUSA_DO_LIMITE: "O limite do contrato recusou a operação.",
}


class RespostaJsonExata(JSONResponse):
    """JSONResponse que escreve Decimal como número JSON exato (nem float, nem string)."""

    def render(self, content: Any) -> bytes:
        return simplejson.dumps(content, use_decimal=True, ensure_ascii=False).encode("utf-8")


# --- Modelos de entrada (schema PedidoDeAutorizacao do contrato) ---------------------------------


def _numero_exato(valor: object) -> Decimal:
    """Só número JSON: string, bool e float binário não passam (o parse já entrega Decimal/int)."""
    if isinstance(valor, bool) or not isinstance(valor, int | Decimal):
        raise ValueError("valor precisa ser um número JSON")
    return Decimal(valor)


class GeoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uf: Annotated[str | None, Field(pattern=r"^[A-Z]{2}$")] = None
    municipio_ibge: Annotated[str | None, Field(pattern=r"^[0-9]{7}$")] = None


class MetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canal: Literal["APP", "WEB", "POS", "PIX", "BACKOFFICE"] | None = None
    id_rastreio_canal: Annotated[str | None, Field(max_length=64, pattern=_ID_FORMA_LIVRE)] = None
    geo: GeoRequest | None = None


class PedidoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_contrato: str | None = None
    id_conta: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,16}$")]
    valor: Annotated[Decimal, BeforeValidator(_numero_exato)]
    moeda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    tipo_operacao: Literal["DEBITO", "CREDITO", "RESERVA", "ESTORNO"]
    id_estabelecimento: Annotated[str | None, Field(max_length=64, pattern=_ID_FORMA_LIVRE)] = None
    id_autorizacao_original: Annotated[str | None, Field(pattern=_UUID_REGEX)] = None
    metadata: MetadataRequest | None = None


class _Rejeicao(Exception):
    """Requisição recusada na borda; vira um problema (nunca vaza para o framework)."""

    def __init__(
        self,
        codigo: str,
        detail: str,
        extras: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.detail = detail
        self.extras: Mapping[str, object] = extras or {}
        self.headers: Mapping[str, str] | None = headers


# --- Helpers -------------------------------------------------------------------------------------


def _erro_de_campo(campo: str, mensagem: str) -> dict[str, str]:
    return {"campo": campo, "mensagem": mensagem}


def _rejeicao_de_dominio(campo: str, mensagem: str) -> _Rejeicao:
    return _Rejeicao(
        "VALIDACAO_DE_DOMINIO", "Pedido inválido.", {"erros": [_erro_de_campo(campo, mensagem)]}
    )


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


def _gateway_request_id(valor: str | None) -> str | None:
    return valor if valor and _GATEWAY_REQUEST_ID.match(valor) else None


def _dinheiro_json(valor: Decimal) -> Decimal:
    return valor.quantize(_DUAS_CASAS)


def _instante_json(instante: datetime) -> str:
    """ISO 8601 UTC com milissegundos: forma única, estável na ida e volta pelo banco."""
    return instante.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _saldos_json(saldos: SaldosDoLimite) -> dict[str, object]:
    return {
        "saldo_reservado": _dinheiro_json(saldos.saldo_reservado),
        "limite_disponivel": _dinheiro_json(saldos.limite_disponivel),
        "limite_utilizado": _dinheiro_json(saldos.limite_utilizado),
        "versao_limite": saldos.versao,
    }


def _aprovada_json(a: Autorizacao) -> dict[str, object]:
    """Schema AutorizacaoAprovada, só com campos gravados (replay idêntico ao original)."""
    assert a.saldos is not None  # toda aprovação grava os saldos
    corpo: dict[str, object] = {
        "id_autorizacao": a.id_autorizacao,
        "id_contrato": a.id_contrato,
        "status": "APROVADA",
        "tipo_operacao": a.tipo.value,
        "valor": _dinheiro_json(a.valor),
        "moeda": a.moeda,
    }
    if a.id_autorizacao_original is not None:
        corpo["id_autorizacao_original"] = a.id_autorizacao_original
    corpo["criada_em"] = _instante_json(a.criada_em)
    corpo["correlation_id"] = a.correlation_id
    corpo.update(_saldos_json(a.saldos))
    return corpo


def _autorizacao_json(a: Autorizacao) -> dict[str, object]:
    """Schema Autorizacao (GET): estado ATUAL."""
    corpo: dict[str, object] = {
        "id_autorizacao": a.id_autorizacao,
        "id_contrato": a.id_contrato,
        "status": a.status.value,
    }
    if a.motivo_recusa is not None:
        corpo["motivo_recusa"] = a.motivo_recusa.value
    corpo["tipo_operacao"] = a.tipo.value
    corpo["valor"] = _dinheiro_json(a.valor)
    corpo["moeda"] = a.moeda
    if a.saldos is not None:
        corpo["saldos"] = _saldos_json(a.saldos)
    corpo["criada_em"] = _instante_json(a.criada_em)
    corpo["atualizada_em"] = _instante_json(a.atualizada_em)
    return corpo


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


def _exigir_client_id(request: Request) -> str:
    # Na AWS o API Gateway valida o token Cognito e injeta X-Client-Id a partir dele; o serviço só é
    # alcançável pelo VPC Link, então o header não é forjável por quem está fora. Localmente o
    # header faz as vezes do token.
    client_id = (request.headers.get("x-client-id") or "").strip()
    if not client_id:
        raise _Rejeicao("NAO_AUTENTICADO", "Identificação do cliente ausente.")
    return client_id


def _chave_de_idempotencia(request: Request) -> str:
    bruta = (request.headers.get("idempotency-key") or "").strip()
    if len(bruta) >= 2 and bruta.startswith('"') and bruta.endswith('"'):
        bruta = bruta[1:-1]
    chave = _uuid_canonico(bruta)
    if chave is None:
        raise _Rejeicao(
            "IDEMPOTENCY_KEY_INVALIDA", "Idempotency-Key ausente ou fora do formato UUID."
        )
    return bruta


def _ler_pedido(corpo: bytes, id_contrato: str) -> PedidoDeAutorizacao:
    """JSON cru -> pedido de domínio. `_Rejeicao` 400 (sintaxe) ou 422 (validação)."""
    try:
        bruto = json.loads(corpo, parse_float=Decimal)
    except (ValueError, RecursionError) as erro:
        raise _Rejeicao("REQUISICAO_MAL_FORMADA", "O corpo não é um JSON válido.") from erro
    try:
        r = PedidoRequest.model_validate(bruto)
    except ValidationError as erro:
        erros = [
            _erro_de_campo(".".join(str(parte) for parte in e["loc"]) or "corpo", e["msg"])
            for e in erro.errors(include_url=False, include_context=False, include_input=False)
        ]
        raise _Rejeicao("VALIDACAO_DE_DOMINIO", "Pedido inválido.", {"erros": erros}) from erro
    if r.id_contrato is not None and r.id_contrato != id_contrato:
        raise _rejeicao_de_dominio("id_contrato", "diferente do id_contrato do path")
    try:
        return PedidoDeAutorizacao(
            id_contrato=id_contrato,
            id_conta=r.id_conta,
            valor=r.valor,
            moeda=r.moeda,
            tipo=TipoOperacao(r.tipo_operacao),
            id_estabelecimento=r.id_estabelecimento,
            id_autorizacao_original=(
                r.id_autorizacao_original.lower() if r.id_autorizacao_original else None
            ),
            metadata=MappingProxyType(
                r.metadata.model_dump(exclude_none=True) if r.metadata else {}
            ),
        )
    except PedidoInvalido as erro:
        raise _rejeicao_de_dominio(erro.campo, erro.mensagem) from erro


def _responder_desfecho(request: Request, desfecho: Desfecho) -> Response:
    a = desfecho.autorizacao
    cabecalhos = {"Idempotent-Replayed": "true"} if desfecho.replay else {}
    if a.resultado is Resultado.APROVADA:
        cabecalhos["Location"] = f"/v1/contratos/{a.id_contrato}/autorizacoes/{a.id_autorizacao}"
        return RespostaJsonExata(
            _aprovada_json(a), status_code=200 if desfecho.replay else 201, headers=cabecalhos
        )
    assert a.motivo_recusa is not None  # recusada sempre tem motivo
    codigo = _CODIGO_DA_RECUSA[a.motivo_recusa]
    extras: dict[str, object] = {}
    if codigo == "LIMITE_INSUFICIENTE":
        extras["id_autorizacao"] = a.id_autorizacao
        if a.saldos is not None:
            extras["limite_disponivel"] = _dinheiro_json(a.saldos.limite_disponivel)
    detalhe = _DETALHE_DO_MOTIVO.get(a.motivo_recusa) or _DETALHE_DA_RECUSA[codigo]
    return _problema(request, codigo, detalhe, extras, cabecalhos)


def _traduzir_excecao(request: Request, erro: Exception) -> Response:
    if isinstance(erro, ChaveReutilizada):
        return _problema(
            request, "IDEMPOTENCY_KEY_REUTILIZADA", "A chave já foi usada com outro pedido."
        )
    if isinstance(erro, EmAndamento):
        return _problema(
            request,
            "REQUISICAO_EM_ANDAMENTO",
            "A mesma chave ainda está em processamento.",
            headers={"Retry-After": str(erro.segundos_para_tentar)},
        )
    if isinstance(erro, EmReconciliacao):
        return _problema(
            request,
            "CONFLITO_DE_ESTADO",
            "Autorização em reconciliação manual: o resultado sai de análise. Não repita; consulte com GET.",
        )
    if isinstance(erro, EncerradaSemResultado):
        return _problema(
            request, "CONFLITO_DE_ESTADO", "A chave foi encerrada sem resultado; use uma nova."
        )
    if isinstance(erro, AutorizacaoOriginalInvalida):
        return _problema(request, "AUTORIZACAO_ORIGINAL_INVALIDA", str(erro))
    if isinstance(erro, ConflitoDeEstado):
        return _problema(request, "CONFLITO_DE_ESTADO", str(erro))
    if isinstance(erro, LimitesIndisponivel):
        return _problema(
            request,
            "SERVICO_INDISPONIVEL",
            "Dependência indisponível; repita com a mesma Idempotency-Key.",
            headers={"Retry-After": "1"},
        )
    _log.error("erro inesperado", exc_info=erro, extra=contexto_de_log())
    return _problema(request, "ERRO_INTERNO", "Falha inesperada. Informe o correlation_id.")


# --- App -----------------------------------------------------------------------------------------


def criar_app(  # noqa: PLR0915 - rotas registradas em closure, como no serviço limites
    autorizar: AutorizarTransacao, consultar: ConsultarAutorizacao, max_em_voo: int
) -> FastAPI:
    app = FastAPI(
        title="API de Autorização de Transações",
        version="1.0.0",
        openapi_url=None,  # o contrato é contracts/openapi/autorizacao-v1.yaml, não a spec gerada
        docs_url=None,
        redoc_url=None,
    )
    em_voo = 0
    trava = threading.Lock()

    @app.middleware("http")
    async def contexto_da_requisicao(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Correlação em TODA resposta, contexto de observabilidade e load shedding."""
        nonlocal em_voo
        correlation_id = _uuid_canonico(request.headers.get("x-correlation-id")) or str(uuid4())
        traceparent = request.headers.get("traceparent")
        gateway_request_id = _gateway_request_id(request.headers.get("x-gateway-request-id"))
        request.state.correlation_id = correlation_id
        request.state.trace_id = _trace_id(traceparent)
        with trava:
            em_voo += 1
            sobrecarregado = em_voo > max_em_voo and request.url.path != "/saude"
        tokens = definir_contexto(correlation_id, traceparent, gateway_request_id)
        try:
            if sobrecarregado:
                resposta: Response = _problema(
                    request,
                    "SERVICO_INDISPONIVEL",
                    "Serviço sobrecarregado; tente novamente.",
                    headers={"Retry-After": "1"},
                )
            else:
                try:
                    resposta = await call_next(request)
                except Exception:
                    _log.exception("erro inesperado", extra=contexto_de_log())
                    resposta = _problema(
                        request, "ERRO_INTERNO", "Falha inesperada. Informe o correlation_id."
                    )
        finally:
            limpar_contexto(tokens)
            with trava:
                em_voo -= 1
        resposta.headers["X-Correlation-Id"] = correlation_id
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

    @app.post("/v1/contratos/{id_contrato}/autorizacoes")
    async def autorizar_transacao(request: Request, id_contrato: IdContrato) -> Response:
        try:
            # Ordem = precedência do catálogo: autenticação, mídia, chave, sintaxe, domínio.
            client_id = _exigir_client_id(request)
            tipo_de_midia = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if tipo_de_midia != "application/json":
                raise _Rejeicao(
                    "TIPO_DE_MIDIA_NAO_SUPORTADO", "Content-Type precisa ser application/json."
                )
            chave = _chave_de_idempotencia(request)
            pedido = _ler_pedido(await request.body(), id_contrato)
        except _Rejeicao as rejeicao:
            return _problema(
                request, rejeicao.codigo, rejeicao.detail, rejeicao.extras, rejeicao.headers
            )
        escopo = EscopoDeIdempotencia(client_id, "POST", request.url.path, chave)
        contexto = Contexto(request.state.correlation_id, request.headers.get("traceparent"))
        try:
            desfecho = await run_in_threadpool(autorizar.executar, pedido, escopo, contexto)
        except Exception as erro:
            return _traduzir_excecao(request, erro)
        return _responder_desfecho(request, desfecho)

    @app.get("/v1/contratos/{id_contrato}/autorizacoes/{id_autorizacao}")
    async def consultar_autorizacao(
        request: Request, id_contrato: IdContrato, id_autorizacao: str
    ) -> Response:
        try:
            client_id = _exigir_client_id(request)
        except _Rejeicao as rejeicao:
            return _problema(request, rejeicao.codigo, rejeicao.detail)
        try:
            autorizacao = await run_in_threadpool(
                consultar.executar, id_contrato, id_autorizacao, client_id
            )
        except AutorizacaoNaoEncontrada:
            return _problema(
                request, "AUTORIZACAO_NAO_ENCONTRADA", "Autorização inexistente neste contrato."
            )
        return RespostaJsonExata(_autorizacao_json(autorizacao))

    return app
