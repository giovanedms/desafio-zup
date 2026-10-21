"""CLI do legado simulado: publica registros de limite na fila `legado-contratos` (LocalStack).

    python -m legado_mock enviar --contrato X --conta Y --produto 01 --limite 1500.00 --situacao A --versao N
    python -m legado_mock cenario <aumento|bloqueio|fora-de-ordem|carga-inicial>

Segurança: endpoint SEMPRE local (AWS_ENDPOINT_URL, default http://127.0.0.1:4566) e credenciais
fictícias explícitas. O shell do operador exporta credenciais reais; nada aqui confia no ambiente.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config

from legado_mock.registro import RegistroLC, formatar

# Endpoint local por allowlist exata, sem parser de URL: urlparse e o urllib3 do botocore discordam em casos como
# `http://exemplo.com\@localhost/`, que um leria como local e o outro discaria para fora. Porta de 1 a 65535;
# ASCII para o K de Kelvin não casar com `k` no IGNORECASE. A mesma regex em todo guard do repositório
# (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_LOCAL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)
FILA_PADRAO = "legado-contratos"
CONTRATO_DE_DEMO = ("CTR-DEMO-0001", "0001-00000001-1", "01")


def cliente_sqs() -> Any:
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or "http://127.0.0.1:4566"
    if not ENDPOINT_LOCAL.fullmatch(endpoint):
        sys.exit(f"recusado: endpoint não é local ({endpoint})")
    opcoes = Config(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
        retries={"mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    return boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=opcoes,
    )


def _registro(
    contrato: str, conta: str, produto: str, *, limite: str, situacao: str, versao: int
) -> RegistroLC:
    return RegistroLC(contrato, conta, produto, Decimal(limite), situacao, versao)


def _aumento(versao: int) -> list[RegistroLC]:
    return [_registro(*CONTRATO_DE_DEMO, limite="2500.00", situacao="A", versao=versao)]


def _bloqueio(versao: int) -> list[RegistroLC]:
    return [_registro(*CONTRATO_DE_DEMO, limite="2500.00", situacao="B", versao=versao)]


def _fora_de_ordem(versao: int) -> list[RegistroLC]:
    """Entrega v+3, v+2, v+4 (nessa ordem): a v+2 chega atrasada e deve ser ignorada pela ACL."""
    ordem = [(versao + 3, "3500.00"), (versao + 2, "3000.00"), (versao + 4, "4000.00")]
    return [
        _registro(*CONTRATO_DE_DEMO, limite=limite, situacao="A", versao=v) for v, limite in ordem
    ]


def _carga_inicial(versao: int) -> list[RegistroLC]:
    return [
        _registro(
            f"CTR-LEGADO-{n:04d}",
            f"0002-{n:08d}-0",
            "01",
            limite=f"{n * 1000}.00",
            situacao="A",
            versao=versao,
        )
        for n in range(1, 4)
    ]


# nome → (construtor dado a versão inicial, versão inicial padrão)
CENARIOS: dict[str, tuple[Callable[[int], list[RegistroLC]], int]] = {
    "aumento": (_aumento, 2),
    "bloqueio": (_bloqueio, 3),
    "fora-de-ordem": (_fora_de_ordem, 2),  # envia v5, v4, v6
    "carga-inicial": (_carga_inicial, 1),
}


def enviar(sqs: Any, fila: str, registros: Sequence[RegistroLC]) -> None:
    url = sqs.get_queue_url(QueueName=fila)["QueueUrl"]
    for r in registros:
        sqs.send_message(QueueUrl=url, MessageBody=formatar(r))
        print(f"enviado {r.contrato} v{r.versao} limite={r.limite} situacao={r.situacao}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="legado_mock", description=__doc__)
    p.add_argument("--fila", default=os.environ.get("FILA_LEGADO") or FILA_PADRAO)
    sub = p.add_subparsers(dest="comando", required=True)
    e = sub.add_parser("enviar", help="envia um registro LC")
    e.add_argument("--contrato", required=True)
    e.add_argument("--conta", required=True)
    e.add_argument("--produto", required=True, choices=sorted({"01", "02", "03", "04"}))
    e.add_argument("--limite", required=True, type=Decimal)
    e.add_argument("--situacao", required=True, choices=["A", "B", "E"])
    e.add_argument("--versao", required=True, type=int)
    c = sub.add_parser("cenario", help="envia um cenário pronto")
    c.add_argument("nome", choices=sorted(CENARIOS))
    c.add_argument(
        "--versao",
        type=int,
        help="versão inicial do cenário (default do cenário; reexecutar exige versão maior)",
    )
    return p


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.comando == "enviar":
            registros = [
                RegistroLC(
                    args.contrato, args.conta, args.produto, args.limite, args.situacao, args.versao
                )
            ]
        else:
            construtor, versao_padrao = CENARIOS[args.nome]
            registros = construtor(args.versao or versao_padrao)
    except ValueError as erro:
        sys.exit(f"registro inválido: {erro}")
    enviar(cliente_sqs(), args.fila, registros)


if __name__ == "__main__":
    main()
