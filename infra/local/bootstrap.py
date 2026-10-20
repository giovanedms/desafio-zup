"""Cria os recursos AWS do ambiente local na LocalStack e semeia contratos de demonstração.

Idempotente: pode rodar quantas vezes quiser. Fonte única das definições de tabela: os próprios
adapters (`definicao_da_tabela`, `definicoes_das_tabelas`) — os mesmos usados nos testes de integração.
O Terraform espelha estes recursos na AWS.

Uso: `uv run python infra/local/bootstrap.py` (o `make up` já chama).
Segurança: endpoint SEMPRE local e credenciais fictícias explícitas — o shell do operador exporta
credenciais reais, então nada aqui confia no ambiente.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import boto3
from autorizacao.adapters.outbound.dynamodb import definicoes_das_tabelas
from botocore.config import Config
from botocore.exceptions import ClientError
from contabilizacao.adapters.outbound.dynamodb import (
    definicoes_das_tabelas as definicoes_contabilizacao,
)
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB, definicao_da_tabela
from limites.application.portas import ContratoJaExiste
from limites.domain.limite import Limite, StatusContrato, TipoProduto

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://127.0.0.1:4566")
REGIAO = "sa-east-1"
BUS = "financas"
SOURCE_DO_BUS = "financas.autorizacao"
SOURCE_DOS_LIMITES = "financas.limites"  # LimiteAlterado (CDC da tabela limites)
FILA_DO_LEGADO = "legado-contratos"  # feed do core legado, consumido pela ACL do BC Limites

# Consumidor → tipos de evento que ele assina (roteamento por regra no bus).
ASSINATURAS: dict[str, list[str]] = {
    "contabilizacao": [
        "TransacaoAutorizada",
        "TransacaoEstornada",
        "ReservaExpirada",
        "ReservaCapturada",
    ],
    "antifraude": ["TransacaoAutorizada", "TransacaoRecusada"],
    "notificacoes": ["TransacaoAutorizada", "TransacaoRecusada", "TransacaoEstornada"],
}

CONTRATOS_DE_DEMO = [
    ("CTR-DEMO-0001", "0001-00000001-1", TipoProduto.CREDITO, "1000.00", StatusContrato.ATIVO),
    ("CTR-DEMO-0002", "0001-00000002-2", TipoProduto.CREDITO, "5000.00", StatusContrato.ATIVO),
    (
        "CTR-DEMO-0003",
        "0001-00000003-3",
        TipoProduto.CONSORCIO,
        "1000.00",
        StatusContrato.BLOQUEADO,
    ),
    *[
        (
            f"CTR-CARGA-{n:04d}",
            f"0001-{n + 8:08d}-9",
            TipoProduto.CREDITO,
            "99999999.00",
            StatusContrato.ATIVO,
        )
        for n in range(1, 21)
    ],
]


def cliente(servico: str) -> Any:
    if urlparse(ENDPOINT).hostname not in {"127.0.0.1", "localhost", "localstack"}:
        sys.exit(f"recusado: endpoint não é local ({ENDPOINT})")
    return boto3.client(
        servico,
        endpoint_url=ENDPOINT,
        region_name=REGIAO,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"mode": "standard"}),
    )


def criar_tabela(dynamodb: Any, definicao: dict[str, Any], com_ttl: bool) -> None:
    nome = definicao["TableName"]
    try:
        dynamodb.create_table(**definicao)
        dynamodb.get_waiter("table_exists").wait(TableName=nome)
        print(f"  tabela {nome}: criada")
    except dynamodb.exceptions.ResourceInUseException:
        print(f"  tabela {nome}: já existe")
        garantir_indices(dynamodb, definicao)
    if com_ttl:
        try:
            dynamodb.update_time_to_live(
                TableName=nome, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}
            )
        except ClientError as erro:  # TTL já habilitado
            if "already enabled" not in str(erro).lower():
                raise


def garantir_indices(dynamodb: Any, definicao: dict[str, Any]) -> None:
    """Tabela criada por uma versão anterior do bootstrap: acrescenta os GSIs que faltam (idempotente)."""
    nome = definicao["TableName"]
    existentes = {
        g["IndexName"]
        for g in dynamodb.describe_table(TableName=nome)["Table"].get("GlobalSecondaryIndexes", [])
    }
    for indice in definicao.get("GlobalSecondaryIndexes", []):
        if indice["IndexName"] in existentes:
            continue
        dynamodb.update_table(
            TableName=nome,
            AttributeDefinitions=definicao["AttributeDefinitions"],
            GlobalSecondaryIndexUpdates=[{"Create": indice}],
        )
        print(f"  tabela {nome}: índice {indice['IndexName']} criado")


def criar_fila_com_dlq(sqs: Any, nome: str) -> str:
    dlq_url = sqs.create_queue(
        QueueName=f"{nome}-dlq", Attributes={"MessageRetentionPeriod": "1209600"}
    )["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url = sqs.create_queue(
        QueueName=nome,
        Attributes={
            "MessageRetentionPeriod": "1209600",  # 14 dias: absorve consumidor parado
            "VisibilityTimeout": "30",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
        },
    )["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    print(f"  fila {nome} (+ DLQ): ok")
    return str(arn)


def main() -> None:
    print(f"bootstrap em {ENDPOINT}")
    dynamodb, sqs, events = cliente("dynamodb"), cliente("sqs"), cliente("events")

    criar_tabela(dynamodb, definicao_da_tabela("limites"), com_ttl=False)
    for definicao in definicoes_das_tabelas("autorizacoes", "autorizacoes-outbox"):
        criar_tabela(dynamodb, definicao, com_ttl=True)
    # lançamentos: livro-razão sem TTL; inbox: TTL de 30 dias (> retenção de 14 dias do SQS)
    lancamentos, inbox = definicoes_contabilizacao(
        "contabilizacao-lancamentos", "contabilizacao-inbox"
    )
    criar_tabela(dynamodb, lancamentos, com_ttl=False)
    criar_tabela(dynamodb, inbox, com_ttl=True)

    try:
        events.create_event_bus(Name=BUS)
        print(f"  bus {BUS}: criado")
    except events.exceptions.ResourceAlreadyExistsException:
        print(f"  bus {BUS}: já existe")

    for consumidor, tipos in ASSINATURAS.items():
        arn = criar_fila_com_dlq(sqs, consumidor)
        regra = f"para-{consumidor}"
        events.put_rule(
            Name=regra,
            EventBusName=BUS,
            EventPattern=json.dumps({"source": [SOURCE_DO_BUS], "detail-type": tipos}),
            State="ENABLED",
        )
        events.put_targets(Rule=regra, EventBusName=BUS, Targets=[{"Id": consumidor, "Arn": arn}])
        print(f"  regra {regra} → {consumidor}: {', '.join(tipos)}")

    # LimiteAlterado (source financas.limites) → notificacoes
    events.put_rule(
        Name="limite-alterado-para-notificacoes",
        EventBusName=BUS,
        EventPattern=json.dumps(
            {"source": [SOURCE_DOS_LIMITES], "detail-type": ["LimiteAlterado"]}
        ),
        State="ENABLED",
    )
    arn_notificacoes = sqs.get_queue_attributes(
        QueueUrl=sqs.get_queue_url(QueueName="notificacoes")["QueueUrl"],
        AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]
    events.put_targets(
        Rule="limite-alterado-para-notificacoes",
        EventBusName=BUS,
        Targets=[{"Id": "notificacoes", "Arn": arn_notificacoes}],
    )
    print("  regra limite-alterado-para-notificacoes → notificacoes: LimiteAlterado")

    criar_fila_com_dlq(sqs, FILA_DO_LEGADO)

    repositorio = RepositorioDeLimitesDynamoDB(dynamodb, "limites")
    for id_contrato, id_conta, produto, concedido, status in CONTRATOS_DE_DEMO:
        try:
            repositorio.criar(
                Limite.novo(id_contrato, id_conta, produto, Decimal(concedido), status=status)
            )
            print(f"  contrato {id_contrato}: concedido {concedido} ({status.value})")
        except ContratoJaExiste:
            print(f"  contrato {id_contrato}: já existe")
    print("bootstrap concluído")


if __name__ == "__main__":
    main()
