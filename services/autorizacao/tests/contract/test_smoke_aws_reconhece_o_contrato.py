"""As verificações do smoke na AWS (`tests/e2e/aws.py borda`) aceitam o que o contrato declara, e só isso.

O smoke não roda sem a conta; aqui ficam as funções que ele usa para julgar as respostas. O 422 é conferido contra
a resposta REAL deste serviço (se o smoke exigisse outra forma, nunca passaria na AWS); o 429 contra a forma das
Gateway Responses `THROTTLED`/`QUOTA_EXCEEDED` (`infra/terraform/modules/api/main.tf`). Fica aqui porque
`tests/e2e/` não é coletado pelo pytest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("smoke_aws", RAIZ / "tests/e2e/aws.py")
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)

_spec_dubles = importlib.util.spec_from_file_location(
    "dubles_http_publico_smoke", Path(__file__).resolve().parents[1] / "unit/test_http_publico.py"
)
assert _spec_dubles and _spec_dubles.loader
_dubles = importlib.util.module_from_spec(_spec_dubles)
sys.modules[_spec_dubles.name] = _dubles
_spec_dubles.loader.exec_module(_dubles)


def _resposta(
    status: int, content_type: str, corpo: dict[str, Any], **headers: str
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": content_type, **headers},
        content=json.dumps(corpo).encode(),
    )


def test_o_422_real_do_servico_para_valor_zero_passa_no_smoke() -> None:
    cliente = _dubles._cliente(_dubles.LimitesFalso())

    resposta = cliente.post(_dubles.URL, content=_dubles._corpo("0"), headers=_dubles._cabecalhos())

    assert smoke.eh_422_do_servico(resposta)


@pytest.mark.parametrize(
    "resposta",
    [
        # o que o gateway responderia se validasse o corpo (BAD_REQUEST_BODY)
        _resposta(400, "application/problem+json", {"codigo": "REQUISICAO_MAL_FORMADA"}),
        _resposta(422, "application/json", {"codigo": "VALIDACAO_DE_DOMINIO"}),
        _resposta(422, "application/problem+json", {"codigo": "IDEMPOTENCY_KEY_REUTILIZADA"}),
        httpx.Response(
            422, headers={"content-type": "application/problem+json"}, content=b"<html>"
        ),
    ],
    ids=["400-do-gateway", "sem-problem-json", "outro-codigo", "corpo-nao-json"],
)
def test_o_smoke_recusa_422_que_nao_e_do_servico(resposta: httpx.Response) -> None:
    assert not smoke.eh_422_do_servico(resposta)


@pytest.mark.parametrize(
    "content_type", ["application/problem+json", "application/problem+json; charset=utf-8"]
)
def test_o_smoke_aceita_o_429_das_gateway_responses(content_type: str) -> None:
    throttled = _resposta(
        429, content_type, {"codigo": "LIMITE_DE_REQUISICOES"}, **{"Retry-After": "1"}
    )

    assert smoke.eh_429_do_contrato(throttled)


@pytest.mark.parametrize(
    "resposta",
    [
        _resposta(429, "application/problem+json", {"codigo": "LIMITE_DE_REQUISICOES"}),
        _resposta(
            429, "application/json", {"message": "Too Many Requests"}, **{"Retry-After": "1"}
        ),
        _resposta(
            403, "application/problem+json", {"codigo": "SEM_PERMISSAO"}, **{"Retry-After": "1"}
        ),
    ],
    ids=["sem-retry-after", "sem-problem-json", "nao-e-429"],
)
def test_o_smoke_recusa_429_fora_do_contrato(resposta: httpx.Response) -> None:
    assert not smoke.eh_429_do_contrato(resposta)
