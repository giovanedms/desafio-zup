"""Repositório contábil no DynamoDB: inbox (idempotência) + lançamentos, numa única transação.

Tabelas:
  - `contabilizacao-inbox`: item `INBOX#<source>|<id>` por evento já contabilizado. TTL de 30 dias,
    maior que a retenção de 14 dias do SQS: nenhuma reentrega possível sobrevive à inbox.
  - `contabilizacao-lancamentos`: item `LANC#<id_lancamento>` por lançamento (livro-razão; sem TTL,
    a retenção contábil é regulatória).

Os dois Puts saem na MESMA TransactWriteItems: ou há inbox + lançamento, ou nenhum. `ClientRequestToken`
(hash do conteúdo) torna idempotente o retry de um commit incerto.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from contabilizacao.domain.lancamento import Lancamento

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

RETENCAO_DA_INBOX = timedelta(days=30)
_serializar = TypeSerializer().serialize
_desserializar = TypeDeserializer().deserialize


class RepositorioContabilDynamoDB:
    def __init__(self, cliente: DynamoDBClient, tabela_lancamentos: str, tabela_inbox: str) -> None:
        self._cliente = cliente
        self._lancamentos = tabela_lancamentos
        self._inbox = tabela_inbox

    def registrar(self, lancamento: Lancamento, chave_da_inbox: str) -> bool:
        itens: list[Any] = [
            {
                "Put": {
                    "TableName": self._inbox,
                    "Item": _item_da_inbox(chave_da_inbox),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self._lancamentos,
                    "Item": _item_do_lancamento(lancamento),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        # Token = hash do conteúdo + nonce POR CHAMADA. O token só precisa proteger o retry do SDK (mesmo
        # request, mesmo token). Sem nonce, a reentrega do mesmo evento em <10 min reaproveitaria o token e
        # o DynamoDB devolveria "sucesso" idempotente (ou IdempotentParameterMismatch, pois o `ttl` muda)
        # em vez de falhar a condição da inbox: a duplicata seria reportada como CONTABILIZADO.
        token = hashlib.sha256(
            json.dumps([itens, uuid.uuid4().hex], sort_keys=True).encode()
        ).hexdigest()[:36]
        try:
            self._cliente.transact_write_items(TransactItems=itens, ClientRequestToken=token)
        except self._cliente.exceptions.TransactionCanceledException as erro:
            motivos = [m.get("Code") for m in erro.response.get("CancellationReasons", [])]
            if motivos and motivos[0] == "ConditionalCheckFailed":
                return False  # inbox já tinha a chave: reentrega
            raise
        return True

    def listar(self, limite: int) -> list[dict[str, Any]]:
        """Scan só para demo/inspeção; nunca no caminho quente."""
        itens = self._cliente.scan(TableName=self._lancamentos, Limit=limite).get("Items", [])
        return [{k: _desserializar(v) for k, v in item.items()} for item in itens]


def definicoes_das_tabelas(tabela_lancamentos: str, tabela_inbox: str) -> list[dict[str, Any]]:
    """Fonte única para bootstrap local e testes; o Terraform espelha (TTL da inbox é ligado à parte)."""
    base = {
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
    }
    return [{"TableName": tabela_lancamentos, **base}, {"TableName": tabela_inbox, **base}]


def _item_da_inbox(chave: str) -> dict[str, Any]:
    expira = datetime.now(UTC) + RETENCAO_DA_INBOX
    return {
        "pk": _serializar(f"INBOX#{chave}"),
        "ttl": _serializar(int(expira.timestamp())),
    }


def _item_do_lancamento(lancamento: Lancamento) -> dict[str, Any]:
    bruto: dict[str, object] = {
        "pk": f"LANC#{lancamento.id_lancamento}",
        "id_lancamento": lancamento.id_lancamento,
        "id_evento_origem": lancamento.id_evento_origem,
        "tipo_evento_origem": lancamento.tipo_evento_origem,
        "id_contrato": lancamento.id_contrato,
        "data_contabil": lancamento.data_contabil.isoformat(),
        "ocorrido_em": lancamento.ocorrido_em.isoformat(),
        "historico": lancamento.historico,
        "partidas": [
            {"conta": p.conta.codigo, "natureza": p.natureza.value, "valor": p.valor}
            for p in lancamento.partidas
        ],
    }
    return {k: _serializar(v) for k, v in bruto.items()}
