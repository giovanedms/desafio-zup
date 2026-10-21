"""Inbox do consumidor no DynamoDB: reivindicação com prazo e conclusão, por (source, id) do CloudEvent.

Item `INBOX#<source>|<id>`:
  - `estado`: EM_PROCESSAMENTO (reivindicado, efeito em curso) ou CONCLUIDO (efeito emitido);
  - `prazo_ms`: até quando a reivindicação vale (o fim da invocação que a fez); some ao concluir;
  - `ttl`: 30 dias, mais que a retenção de 14 dias do SQS e o mesmo do archive do bus: reentrega e replay dentro
    desse tempo encontram o item. Item vencido que o DynamoDB ainda não apagou continua barrando (é duplicata mesmo).

Custo por evento: novo = PutItem + UpdateItem (2 WRU, item < 1 KB); duplicata = 1 PutItem condicional que falha
(1 WRU: escrita condicional que falha também consome). `ReturnValuesOnConditionCheckFailure` devolve o item na
própria falha, sem uma leitura a mais para saber se é duplicata ou reivindicação viva.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from consumidores.nucleo import Situacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

RETENCAO = timedelta(days=30)
EM_PROCESSAMENTO = "EM_PROCESSAMENTO"
CONCLUIDO = "CONCLUIDO"
# Nomes por placeholder: nenhum atributo da inbox precisa conferir a lista de palavras reservadas do DynamoDB.
_NOMES = {"#estado": "estado", "#prazo": "prazo_ms"}


class InboxDynamoDB:
    def __init__(self, cliente: DynamoDBClient, tabela: str) -> None:
        self._cliente = cliente
        self._tabela = tabela

    def reivindicar(self, chave: str, agora_ms: int, prazo_ms: int) -> Situacao:
        expira_s = agora_ms // 1000 + int(RETENCAO.total_seconds())
        try:
            self._cliente.put_item(
                TableName=self._tabela,
                Item={
                    "pk": {"S": _pk(chave)},
                    "estado": {"S": EM_PROCESSAMENTO},
                    "prazo_ms": {"N": str(prazo_ms)},
                    "ttl": {"N": str(expira_s)},
                },
                # Chave nova, ou reivindicação anterior vencida sem conclusão (invocação que morreu no meio).
                ConditionExpression=(
                    "attribute_not_exists(pk) OR (#estado = :em_processamento AND #prazo < :agora)"
                ),
                ExpressionAttributeNames=_NOMES,
                ExpressionAttributeValues={
                    ":em_processamento": {"S": EM_PROCESSAMENTO},
                    ":agora": {"N": str(agora_ms)},
                },
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except self._cliente.exceptions.ConditionalCheckFailedException as erro:
            atual: Any = erro.response.get("Item") or {}  # ALL_OLD: o item que barrou a condição
            if atual.get("estado", {}).get("S") == CONCLUIDO:
                return Situacao.CONCLUIDO
            return Situacao.EM_ANDAMENTO
        return Situacao.NOVO

    def concluir(self, chave: str) -> None:
        self._cliente.update_item(
            TableName=self._tabela,
            Key={"pk": {"S": _pk(chave)}},
            UpdateExpression="SET #estado = :concluido REMOVE #prazo",
            # Sem o item (apagado à mão), o UpdateItem criaria um CONCLUIDO sem nunca ter sido reivindicado.
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames=_NOMES,
            ExpressionAttributeValues={":concluido": {"S": CONCLUIDO}},
        )


def definicao_da_tabela(nome: str) -> dict[str, Any]:
    """Fonte única para os testes de integração; o Terraform (`modules/lambdas`) espelha, com o TTL em `ttl`."""
    return {
        "TableName": nome,
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
    }


def _pk(chave: str) -> str:
    return f"INBOX#{chave}"
