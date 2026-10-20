"""CLI do legado simulado: publica registros de limite na fila `legado-contratos` (LocalStack).

    python -m legado_mock enviar --contrato X --conta Y --produto 01 --limite 1500.00 --situacao A --versao N
    python -m legado_mock cenario <aumento|bloqueio|fora-de-ordem|carga-inicial>

Segurança: endpoint SEMPRE local (AWS_ENDPOINT_URL, default http://127.0.0.1:4566) e credenciais
fictícias explícitas. O shell do operador exporta credenciais reais; nada aqui confia no ambiente.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config

from legado_mock.registro import RegistroLC, formatar

HOSTS_LOCAIS = {"127.0.0.1", "localhost", "localstack"}
FILA_PADRAO = "legado-contratos"
CONTRATO_DE_DEMO = ("CTR-DEMO-0001", "0001-00000001-1", "01")


def cliente_sqs() -> Any:
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or "http://127.0.0.1:4566"
    if urlparse(endpoint).hostname not in HOSTS_LOCAIS:
        sys.exit(f"recusado: endpoint não é local ({endpoint})")
    return boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"mode": "standard"}),
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
