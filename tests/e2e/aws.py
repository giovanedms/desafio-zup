"""Verificação da janela na AWS (sandbox): carga de contratos pela ACL, borda do API Gateway, corrida e contabilização.

Uso (depois do `make deploy`):
    AWS_PROFILE=desafio-zup uv run python tests/e2e/aws.py contratos [N] [PREFIXO]   # legado → SQS → limites-acl
    AWS_PROFILE=desafio-zup uv run python tests/e2e/aws.py borda       # 201, replay, 401, 422 (valor 0), 429 (rajada)
    AWS_PROFILE=desafio-zup uv run python tests/e2e/aws.py corrida     # 100 reservas paralelas + lançamentos contábeis

`borda` sai com 1 se qualquer item divergir: 1ª autorização 201; replay 200 com corpo idêntico; sem token 401
application/problem+json (gateway); `valor: 0` 422 application/problem+json com `codigo` VALIDACAO_DE_DOMINIO (o serviço
é a autoridade do corpo, o gateway não o valida); rajada de 40 chamadas SIMULTÂNEAS pelo `canal-app` (plano-restrito:
5 rps, burst 10) com ao menos um 429 application/problem+json com `Retry-After` (até 3 rajadas: o throttle do
gateway é melhor-esforço).

`corrida` sai com 1 se: os status não forem exatamente {201: 10, 402: 90}; o item do contrato no DynamoDB não terminar
com disponível 0; ou, em até 120 s, as 10 reservas aprovadas não tiverem cada uma UM lançamento na tabela
`contabilizacao-lancamentos` (outbox → Stream → relay → bus → regra → fila → Lambda de contabilização, na conta real).
É o mesmo oráculo da seção 6 da demo local, agora na AWS.

Todo pedido usa a conta do contrato carregado pelo legado (`CONTA`): conta divergente é recusada (CONTA_DIVERGENTE).

Segredos (client secrets do Cognito, API keys) vêm de `terraform output -json` e NUNCA são impressos.
Local: o profile precisa ser explícito (o shell do operador exporta credenciais de OUTRA conta, que são removidas).
Na esteira (GitHub Actions + OIDC): sem profile, as credenciais temporárias vêm do ambiente.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import httpx
from limites.adapters.inbound.legado.traducao import formatar
from limites.domain.legado import AtualizacaoDoLegado
from limites.domain.limite import StatusContrato, TipoProduto

AMBIENTE = "infra/terraform/environments/sandbox"
PROBLEM_JSON = "application/problem+json"
RAJADA = 40  # simultâneas no plano-restrito (5 rps, burst 10): bem acima do balde, o gateway estrangula com 429
CONTA = "0001-77777777-7"  # conta dos contratos que o smoke carrega pelo legado
TABELA_LANCAMENTOS = "contabilizacao-lancamentos"
PRAZO_CONTABILIZACAO_S = 120.0  # pipeline assíncrono inteiro, com folga para cold start das Lambdas


def ambiente_aws() -> dict[str, str]:
    """Variáveis para subprocessos (terraform): local, tira as chaves exportadas; na esteira, mantém as do OIDC."""
    if os.environ.get("AWS_PROFILE"):
        chaves = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        return {k: v for k, v in os.environ.items() if k not in chaves}
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return dict(os.environ)
    sys.exit("defina AWS_PROFILE=desafio-zup (o shell exporta credenciais de outra conta)")


def saidas() -> dict[str, Any]:
    env = ambiente_aws()
    bruto = subprocess.run(
        ["terraform", f"-chdir={AMBIENTE}", "output", "-json"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    return {k: v["value"] for k, v in json.loads(bruto).items()}


def sessao() -> boto3.Session:
    ambiente_aws()  # mesma trava: sem profile só dentro da esteira
    return boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None, region_name="sa-east-1"
    )


def token(s: dict[str, Any], canal: str) -> str:
    resposta = httpx.post(
        s["token_endpoint"],
        data={"grant_type": "client_credentials", "scope": "autorizacoes/write autorizacoes/read"},
        auth=(s["cognito_client_ids"][canal], s["cognito_client_secrets"][canal]),
        timeout=10,
    )
    resposta.raise_for_status()
    return str(resposta.json()["access_token"])


def cabecalhos(s: dict[str, Any], canal: str, tk: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tk}",
        "x-api-key": s["api_keys"][canal],
        "Content-Type": "application/json",
    }


def enviar_ao_legado(registros: list[str]) -> None:
    sqs = sessao().client("sqs")
    url = sqs.get_queue_url(QueueName="legado-contratos")["QueueUrl"]
    for inicio in range(0, len(registros), 10):  # SendMessageBatch aceita até 10 mensagens
        lote = registros[inicio : inicio + 10]
        resposta = sqs.send_message_batch(
            QueueUrl=url,
            Entries=[{"Id": str(i), "MessageBody": r} for i, r in enumerate(lote)],
        )
        if resposta.get("Failed"):
            sys.exit(f"SQS recusou {len(resposta['Failed'])} mensagens do lote {inicio // 10}")


def registro(id_contrato: str, concedido: str, versao: int = 1) -> str:
    return formatar(
        AtualizacaoDoLegado(
            id_contrato,
            CONTA,
            TipoProduto.CREDITO,
            Decimal(concedido),
            StatusContrato.ATIVO,
            versao,
            datetime.now(UTC),
        )
    )


def contratos() -> None:
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    prefixo = sys.argv[3] if len(sys.argv) > 3 else "CTR-CARGA-"
    largura = 4 if prefixo == "CTR-CARGA-" else 6
    registros = [registro(f"{prefixo}{i:0{largura}d}", "99999999.00") for i in range(1, n + 1)]
    enviar_ao_legado(registros)
    print(
        f"{len(registros)} registros do legado enviados à fila legado-contratos (ACL cria os contratos)"
    )


def autorizar(
    http: httpx.Client,
    s: dict[str, Any],
    hdr: dict[str, str],
    contrato: str,
    corpo: dict[str, Any],
    *,
    chave: str | None = None,
) -> httpx.Response:
    return http.post(
        f"{s['invoke_url']}/v1/contratos/{contrato}/autorizacoes",
        json=corpo,
        headers={**hdr, "Idempotency-Key": chave or str(uuid.uuid4())},
    )


def media_type(resposta: httpx.Response) -> str:
    return str(resposta.headers.get("content-type", "")).split(";")[0].strip().lower()


def eh_429_do_contrato(resposta: httpx.Response) -> bool:
    """429 como o contrato declara (LimiteDeRequisicoes): problem+json e sempre com Retry-After."""
    return (
        resposta.status_code == 429
        and media_type(resposta) == PROBLEM_JSON
        and bool(resposta.headers.get("Retry-After", "").strip())
    )


def eh_422_do_servico(resposta: httpx.Response) -> bool:
    """422 de validação vindo do SERVIÇO: o gateway, se validasse o corpo, responderia 400 REQUISICAO_MAL_FORMADA."""
    if resposta.status_code != 422 or media_type(resposta) != PROBLEM_JSON:
        return False
    try:
        corpo = resposta.json()
    except ValueError:
        return False
    return isinstance(corpo, dict) and corpo.get("codigo") == "VALIDACAO_DE_DOMINIO"


def rajada(
    http: httpx.Client, s: dict[str, Any], hdr: dict[str, str], contrato: str, corpo: dict[str, Any]
) -> list[httpx.Response]:
    """RAJADA chamadas ao mesmo tempo, cada uma com chave nova (sequenciais ficariam abaixo do balde)."""
    with ThreadPoolExecutor(max_workers=RAJADA) as pool:
        return list(pool.map(lambda _: autorizar(http, s, hdr, contrato, corpo), range(RAJADA)))


def esperar_contrato(contrato: str) -> None:
    tabela = sessao().client("dynamodb")
    for _ in range(60):
        if tabela.get_item(TableName="limites", Key={"id_contrato": {"S": contrato}}).get("Item"):
            return
        time.sleep(1)
    sys.exit(f"contrato {contrato} não apareceu em 60 s (limites-acl de pé?)")


def borda() -> None:
    s = saidas()
    contrato = f"CTR-AWS-{uuid.uuid4().hex[:6].upper()}"
    enviar_ao_legado([registro(contrato, "1000.00")])
    esperar_contrato(contrato)
    corpo = {"id_conta": CONTA, "valor": 150.00, "moeda": "BRL", "tipo_operacao": "RESERVA"}
    with httpx.Client(timeout=15) as http:
        hdr = cabecalhos(s, "canal-pos", token(s, "canal-pos"))
        chave = str(uuid.uuid4())
        r1 = autorizar(http, s, hdr, contrato, corpo, chave=chave)
        r2 = autorizar(http, s, hdr, contrato, corpo, chave=chave)
        print(f"201 esperado: {r1.status_code}  saldo_reservado={r1.json().get('saldo_reservado')}")
        print(
            f"replay: {r2.status_code}  replayed={r2.headers.get('Idempotent-Replayed')}  "
            f"idêntico={r1.content == r2.content}"
        )
        sem_token = http.post(
            f"{s['invoke_url']}/v1/contratos/{contrato}/autorizacoes",
            json=corpo,
            headers={"x-api-key": s["api_keys"]["canal-pos"], "Idempotency-Key": str(uuid.uuid4())},
        )
        print(
            f"sem token: {sem_token.status_code}  {sem_token.headers.get('content-type')}  {sem_token.text[:120]}"
        )
        valor_zero = autorizar(http, s, hdr, contrato, {**corpo, "valor": 0})
        print(
            f"valor 0: {valor_zero.status_code}  {valor_zero.headers.get('content-type')}  "
            f"{valor_zero.text[:120]}"
        )
        restrito = cabecalhos(s, "canal-app", token(s, "canal-app"))
        # O throttle do gateway é melhor-esforço: até 3 rajadas antes de dar falha.
        respostas: list[httpx.Response] = []
        for _ in range(3):
            respostas += rajada(http, s, restrito, contrato, {**corpo, "valor": 0.01})
            if any(eh_429_do_contrato(r) for r in respostas):
                break
        estrangulada = next((r for r in respostas if eh_429_do_contrato(r)), None)
        print(
            f"plano restrito ({len(respostas)} em rajadas de {RAJADA}): "
            f"{dict(Counter(r.status_code for r in respostas))}  "
            f"429 conforme: {sum(eh_429_do_contrato(r) for r in respostas)}"
            + (
                f"  ex.: Retry-After={estrangulada.headers['Retry-After']} {estrangulada.text[:120]}"
                if estrangulada
                else ""
            )
        )
    falhas = [
        o_que
        for ok, o_que in [
            (r1.status_code == 201, "1ª autorização deveria ser 201"),
            (
                r2.status_code == 200 and r1.content == r2.content,
                "replay deveria ser 200 e idêntico",
            ),
            (
                sem_token.status_code == 401
                and sem_token.headers.get("content-type") == "application/problem+json"
                and sem_token.headers.get("www-authenticate") == "Bearer",
                "sem token deveria ser 401 application/problem+json com WWW-Authenticate: Bearer (gateway response)",
            ),
            (
                eh_422_do_servico(valor_zero),
                "valor 0 deveria ser 422 application/problem+json VALIDACAO_DE_DOMINIO (do serviço)",
            ),
            (
                estrangulada is not None,
                f"rajada de {RAJADA} no plano-restrito deveria ter 429 application/problem+json com Retry-After",
            ),
        ]
        if not ok
    ]
    print("✅ borda conforme o contrato" if not falhas else "❌ " + "; ".join(falhas))
    if falhas:
        sys.exit(1)


def autorizar_como_um_canal_correto(
    http: httpx.Client, s: dict[str, Any], hdr: dict[str, str], contrato: str, corpo: dict[str, Any]
) -> tuple[httpx.Response, list[str]]:
    """503/429 são "tente de novo": repete com a MESMA Idempotency-Key depois do Retry-After."""
    chave, vistos = str(uuid.uuid4()), []
    for _ in range(10):
        resposta = autorizar(http, s, hdr, contrato, corpo, chave=chave)
        if resposta.status_code not in (429, 503):
            return resposta, vistos
        vistos.append(f"{resposta.status_code} {resposta.json().get('codigo', '?')}")
        time.sleep(float(resposta.headers.get("Retry-After", "1")))
    return resposta, vistos


def lancamentos_do_contrato(dynamodb: Any, contrato: str) -> list[dict[str, Any]]:
    """Lançamentos do contrato (único por corrida) no razão. Scan paginado: só o smoke faz isto, nunca o serviço."""
    argumentos: dict[str, Any] = {
        "TableName": TABELA_LANCAMENTOS,
        "FilterExpression": "id_contrato = :c",
        "ExpressionAttributeValues": {":c": {"S": contrato}},
        "ProjectionExpression": "id_evento_origem, tipo_evento_origem, historico",
        "ConsistentRead": True,
    }
    itens: list[dict[str, Any]] = []
    while True:
        pagina = dynamodb.scan(**argumentos)
        itens += pagina.get("Items", [])
        if not pagina.get("LastEvaluatedKey"):
            return itens
        argumentos["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]


def faltam_contabilizar(aprovadas: set[str], lancamentos: list[dict[str, Any]]) -> set[str]:
    """Autorizações aprovadas sem lançamento. O histórico da reserva traz o id ("Reserva de limite <id>")."""
    historicos = [str(item.get("historico", {}).get("S", "")) for item in lancamentos]
    return {aut for aut in aprovadas if not any(aut in h for h in historicos)}


def esperar_contabilizacao(
    dynamodb: Any,
    contrato: str,
    aprovadas: set[str],
    *,
    prazo_s: float | None = None,
    intervalo_s: float = 5.0,
    relogio: Callable[[], float] = time.monotonic,
    dormir: Callable[[float], None] = time.sleep,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Espera (com prazo) cada aprovada virar lançamento. Devolve as que faltaram e os lançamentos lidos."""
    fim = relogio() + (PRAZO_CONTABILIZACAO_S if prazo_s is None else prazo_s)
    while True:
        lancamentos = lancamentos_do_contrato(dynamodb, contrato)
        faltam = faltam_contabilizar(aprovadas, lancamentos)
        if not faltam or relogio() >= fim:
            return faltam, lancamentos
        dormir(intervalo_s)


def corrida() -> None:
    s = saidas()
    contrato = f"CTR-AWS-{uuid.uuid4().hex[:6].upper()}"
    enviar_ao_legado([registro(contrato, "1000.00")])
    esperar_contrato(contrato)
    corpo = {"id_conta": CONTA, "valor": 100.00, "moeda": "BRL", "tipo_operacao": "RESERVA"}
    with httpx.Client(timeout=30, limits=httpx.Limits(max_connections=100)) as http:
        hdr = cabecalhos(s, "canal-carga", token(s, "canal-carga"))
        with ThreadPoolExecutor(max_workers=100) as pool:
            resultados = list(
                pool.map(
                    lambda _: autorizar_como_um_canal_correto(http, s, hdr, contrato, corpo),
                    range(100),
                )
            )
    respostas = [r for r, _ in resultados]
    repetidas = Counter(v for _, vistos in resultados for v in vistos)
    if repetidas:
        print(f"repetidas pelo canal (load shedding/cota, mesma chave): {dict(repetidas)}")
    status = Counter(r.status_code for r in respostas)
    dynamodb = sessao().client("dynamodb")
    item = dynamodb.get_item(
        TableName="limites", Key={"id_contrato": {"S": contrato}}, ConsistentRead=True
    )["Item"]
    print(f"AWS — 100 reservas paralelas de R$ 100 em R$ 1.000: {dict(sorted(status.items()))}")
    print(
        f"DynamoDB real: disponivel={item['disponivel']['N']} "
        f"reservado={item['reservado']['N']} versao={item['versao']['N']}"
    )
    invariante = status == {201: 10, 402: 90} and item["disponivel"]["N"] == "0"
    print("✅ invariante preservada" if invariante else "❌ DIVERGÊNCIA na invariante")

    # O fato tem de atravessar o pipeline assíncrono na conta real e virar UM lançamento por reserva aprovada.
    aprovadas = {str(r.json()["id_autorizacao"]) for r in respostas if r.status_code == 201}
    faltam, lancamentos = esperar_contabilizacao(dynamodb, contrato, aprovadas)
    contabilizada = bool(aprovadas) and not faltam and len(lancamentos) == len(aprovadas)
    print(
        f"contabilização: {len(lancamentos)} lançamento(s) em {TABELA_LANCAMENTOS} para "
        f"{len(aprovadas)} aprovada(s); sem lançamento: {sorted(faltam) or 'nenhuma'}"
    )
    print(
        "✅ cada reserva aprovada virou um lançamento (outbox → relay → bus → fila → Lambda)"
        if contabilizada
        else f"❌ DIVERGÊNCIA na contabilização (prazo {PRAZO_CONTABILIZACAO_S:.0f} s)"
    )
    if not (invariante and contabilizada):
        sys.exit(1)


if __name__ == "__main__":
    {"contratos": contratos, "borda": borda, "corrida": corrida}[sys.argv[1]]()
