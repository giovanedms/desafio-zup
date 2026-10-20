"""Repositório de limites no DynamoDB: compila o Plano do domínio numa ÚNICA escrita condicional.

Por que não TransactWriteItems (ADR-0005): transações concorrentes no mesmo item se cancelam em vez
de esperar e não devolvem os valores pós-update. Um UpdateItem condicional de item único é
serializado pelo DynamoDB: 100 reservas paralelas no mesmo contrato aprovam exatamente o que
cabe, sem 409, e a resposta traz o saldo exato (ReturnValues=ALL_NEW).

Commit incerto (timeout com a escrita aplicada): o retry do SDK falha na guarda
`attribute_not_exists(ops.#op)` e o diagnóstico devolve JA_APLICADA — nunca debita duas vezes.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from limites.application.portas import (
    ArmazenamentoIndisponivel,
    ContencaoPersistente,
    ContratoJaExiste,
    ContratoNaoEncontrado,
)
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao, sincronizar
from limites.domain.limite import Limite, StatusContrato, TipoProduto
from limites.domain.operacoes import OperacaoDeLimite, Plano
from limites.domain.resultados import Desfecho, ResultadoDaOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

TENTATIVAS_DE_DIAGNOSTICO = 3
MAX_REMOCOES_POR_ESCRITA = 50  # mantém a UpdateExpression bem abaixo do limite de 4 KB
MAX_CONTRATOS_NO_CACHE = 10_000
TENTATIVAS_DE_SINCRONIZACAO = 5
# Guarda OU mapa de reservas acima disto vira linha de log JSON (metric filters + Contributor Insights por contrato,
# ADR-0016). Os dois mapas moram no item (teto de 400 KB): a guarda vive 5 min, mas um hold vive até 7 dias.
LIMIAR_DE_ALERTA_DA_GUARDA = 200
# Falhas em que o SDK já esgotou os retries e repetir mais tarde é seguro → 503, não 500.
_CODIGOS_TRANSITORIOS = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
    }
)
_log_da_guarda = logging.getLogger("limites.guarda")
_serializar = TypeSerializer().serialize
_desserializar = TypeDeserializer().deserialize


@dataclass
class _Expressao:
    """Acumula placeholders: o DynamoDB recusa nomes/valores declarados e não usados."""

    nomes: dict[str, str] = field(default_factory=dict)
    valores: dict[str, Any] = field(default_factory=dict)

    def nome(self, placeholder: str, atributo: str) -> str:
        self.nomes[placeholder] = atributo
        return placeholder

    def valor(self, placeholder: str, valor: object) -> str:
        self.valores[placeholder] = _serializar(valor)
        return placeholder


def compilar(plano: Plano, agora: datetime, remover_ops: Sequence[str] = ()) -> dict[str, Any]:
    """Pré-condições → ConditionExpression; efeitos → UpdateExpression (uma escrita atômica)."""
    x = _Expressao()
    ops = x.nome("#ops", "ops")

    def reservas() -> str:  # declarado só quando usado (placeholder sobrando = ValidationException)
        return x.nome("#reservas", "reservas")

    op = x.nome("#op", plano.id_operacao)
    condicoes = [
        f"attribute_exists({x.nome('#id', 'id_contrato')})",
        f"attribute_not_exists({ops}.{op})",
    ]
    p, e = plano.precondicoes, plano.efeitos
    if p.contrato_ativo:
        condicoes.append(f"{x.nome('#status', 'status')} = {x.valor(':ativo', 'ATIVO')}")
    if p.disponivel_minimo is not None:
        disponivel = x.nome("#disponivel", "disponivel")
        condicoes.append(f"{disponivel} >= {x.valor(':disp_min', p.disponivel_minimo)}")
    if p.utilizado_minimo is not None:
        utilizado = x.nome("#utilizado", "utilizado")
        condicoes.append(f"{utilizado} >= {x.valor(':util_min', p.utilizado_minimo)}")
    if p.reserva_existente is not None:
        id_reserva, valor = p.reserva_existente
        rx = x.nome("#rx", id_reserva)
        condicoes.append(f"{reservas()}.{rx} = {x.valor(':rx_valor', valor)}")
    if p.reserva_inexistente is not None:
        rn = x.nome("#rn", p.reserva_inexistente)
        condicoes.append(f"attribute_not_exists({reservas()}.{rn})")

    versao = x.nome("#versao", "versao")
    sets = [
        f"{versao} = {versao} + {x.valor(':um', 1)}",
        f"{ops}.{op} = {x.valor(':agora', int(agora.timestamp()))}",
    ]
    if e.delta_utilizado:
        utilizado = x.nome("#utilizado", "utilizado")
        sets.append(f"{utilizado} = {utilizado} + {x.valor(':du', e.delta_utilizado)}")
    if e.delta_reservado:
        reservado = x.nome("#reservado", "reservado")
        sets.append(f"{reservado} = {reservado} + {x.valor(':dr', e.delta_reservado)}")
    delta_disponivel = -(e.delta_utilizado + e.delta_reservado)
    if delta_disponivel:
        disponivel = x.nome("#disponivel", "disponivel")
        sets.append(f"{disponivel} = {disponivel} + {x.valor(':dd', delta_disponivel)}")
    if e.criar_reserva is not None:
        id_reserva, valor = e.criar_reserva
        sets.append(f"{reservas()}.{x.nome('#rc', id_reserva)} = {x.valor(':rc_valor', valor)}")
    expressao = "SET " + ", ".join(sets)
    removes = []
    if e.remover_reserva is not None:
        removes.append(f"{reservas()}.{x.nome('#rr', e.remover_reserva)}")
    # Compactação de carona: tira da guarda as operações vencidas vistas na escrita anterior deste contrato.
    # Remover chave de mapa vencida é seguro sem condição (ninguém mais a consulta) e não custa escrita extra.
    # O id desta operação nunca entra no REMOVE: SET e REMOVE do mesmo caminho é ValidationException (o painel
    # reproduziu com dois retries do mesmo id depois da janela: o 1º põe o id no cache de vencidas).
    removiveis = [v for v in remover_ops if v != plano.id_operacao]
    removes.extend(f"{ops}.{x.nome(f'#x{i}', vencida)}" for i, vencida in enumerate(removiveis))
    if removes:
        expressao += " REMOVE " + ", ".join(removes)
    return {
        "UpdateExpression": expressao,
        "ConditionExpression": " AND ".join(condicoes),
        "ExpressionAttributeNames": x.nomes,
        "ExpressionAttributeValues": x.valores,
    }


@contextmanager
def _falhas_transitorias(id_contrato: str) -> Iterator[None]:
    """Estrangulamento e queda de conexão viram `ArmazenamentoIndisponivel`; o resto passa intacto."""
    try:
        yield
    except ClientError as erro:
        if erro.response.get("Error", {}).get("Code") in _CODIGOS_TRANSITORIOS:
            raise ArmazenamentoIndisponivel(id_contrato) from erro
        raise
    except (
        ConnectTimeoutError,
        ReadTimeoutError,
        EndpointConnectionError,
        ConnectionClosedError,
    ) as erro:
        raise ArmazenamentoIndisponivel(id_contrato) from erro


class RepositorioDeLimitesDynamoDB:
    def __init__(self, cliente: DynamoDBClient, tabela: str) -> None:
        self._cliente = cliente
        self._tabela = tabela
        # contrato → operações vencidas vistas na última resposta (LRU limitado). Cada task tem o seu;
        # remover duas vezes a mesma chave é inócuo, então não precisa de coordenação entre tasks.
        self._vencidas: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._lock_vencidas = threading.Lock()

    def criar(self, limite: Limite) -> None:
        try:
            self._cliente.put_item(
                TableName=self._tabela,
                Item=_para_item(limite),
                ConditionExpression="attribute_not_exists(id_contrato)",
            )
        except self._cliente.exceptions.ConditionalCheckFailedException as erro:
            raise ContratoJaExiste(limite.id_contrato) from erro

    def obter(self, id_contrato: str) -> Limite | None:
        item = self._ler_item(id_contrato)
        return _de_item(item) if item else None

    def aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        with _falhas_transitorias(id_contrato):
            return self._aplicar(id_contrato, operacao, agora)

    def _aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        plano = operacao.planejar()
        for _ in range(TENTATIVAS_DE_DIAGNOSTICO):
            vencidas = self._tirar_vencidas(id_contrato)
            try:
                resposta = self._cliente.update_item(
                    TableName=self._tabela,
                    Key={"id_contrato": {"S": id_contrato}},
                    ReturnValues="ALL_NEW",
                    ReturnValuesOnConditionCheckFailure="ALL_OLD",
                    **compilar(plano, agora, vencidas),
                )
                novo = _de_item(resposta["Attributes"])
                self._guardar_vencidas(id_contrato, novo.operacoes_expiradas(agora))
                _observar_guarda(novo)
                return ResultadoDaOperacao(Desfecho.APLICADA, novo.saldos)
            except self._cliente.exceptions.ConditionalCheckFailedException as erro:
                # ReturnValuesOnConditionCheckFailure=ALL_OLD: o item que falhou vem no erro.
                devolvido = cast("Mapping[str, Any] | None", erro.response.get("Item"))
                item_antigo = devolvido or self._ler_item(id_contrato)
                if not item_antigo:
                    raise ContratoNaoEncontrado(id_contrato) from erro
                antigo = _de_item(item_antigo)
                self._guardar_vencidas(id_contrato, antigo.operacoes_expiradas(agora))
                if antigo.atende(plano):
                    continue  # só acontece no fallback de leitura: o estado mudou nesse meio-tempo
                desfecho, motivo = antigo.diagnosticar(operacao)
                return ResultadoDaOperacao(desfecho, antigo.saldos, motivo)
        raise ContencaoPersistente(id_contrato)

    def _tirar_vencidas(self, id_contrato: str) -> tuple[str, ...]:
        with self._lock_vencidas:
            vencidas = self._vencidas.pop(id_contrato, ())
        return vencidas[:MAX_REMOCOES_POR_ESCRITA]

    def _guardar_vencidas(self, id_contrato: str, vencidas: tuple[str, ...]) -> None:
        if not vencidas:
            return
        with self._lock_vencidas:
            self._vencidas[id_contrato] = vencidas
            self._vencidas.move_to_end(id_contrato)
            while len(self._vencidas) > MAX_CONTRATOS_NO_CACHE:
                self._vencidas.popitem(last=False)

    def sincronizar(self, atualizacao: AtualizacaoDoLegado) -> ResultadoDaSincronizacao:
        """Aplica a atualização do legado: GetItem consistente → regra do domínio → escrita condicional.

        O compare-and-set cobre SÓ os campos do legado (versao_legado, concedido, status). A escrita não
        toca utilizado/reservado/reservas, e o disponível muda por delta (`disponivel + :delta`): reservas
        concorrentes (que também usam expressões comutativas) nunca fazem esta condição falhar.
        Condição falhou = o legado mexeu no item entre a leitura e a escrita → relê e reavalia.
        """
        for _ in range(TENTATIVAS_DE_SINCRONIZACAO):
            atual = self.obter(atualizacao.id_contrato)
            novo, resultado = sincronizar(atual, atualizacao)
            if resultado is ResultadoDaSincronizacao.OBSOLETA:
                return resultado
            try:
                if atual is None:
                    self._cliente.put_item(
                        TableName=self._tabela,
                        Item=_para_item(cast("Limite", novo)),  # CRIADO sempre traz o limite
                        ConditionExpression="attribute_not_exists(id_contrato)",
                    )
                else:
                    self._cliente.update_item(
                        TableName=self._tabela,
                        Key={"id_contrato": {"S": atualizacao.id_contrato}},
                        **_escrita_do_legado(atual, atualizacao, resultado),
                    )
            except self._cliente.exceptions.ConditionalCheckFailedException:
                continue
            return resultado
        raise ContencaoPersistente(atualizacao.id_contrato)

    def _ler_item(self, id_contrato: str) -> Mapping[str, Any] | None:
        with _falhas_transitorias(id_contrato):
            resposta = self._cliente.get_item(
                TableName=self._tabela, Key={"id_contrato": {"S": id_contrato}}, ConsistentRead=True
            )
        return resposta.get("Item")


def _observar_guarda(limite: Limite) -> None:
    """Item crescendo = contrato quente. Uma linha JSON por escrita com guarda OU reservas acima do limiar.

    As reservas entram na condição porque um contrato com muitos holds pendentes (pré-autorização de hotel ou
    posto) enche o item com a guarda vazia: ~45 bytes por reserva, ~9.000 reservas chegam aos 400 KB (ADR-0016).
    """
    if max(len(limite.operacoes_recentes), len(limite.reservas)) >= LIMIAR_DE_ALERTA_DA_GUARDA:
        _log_da_guarda.warning(
            json.dumps(
                {
                    "evento": "guarda_de_idempotencia",
                    "id_contrato": limite.id_contrato,
                    "operacoes_na_guarda": len(limite.operacoes_recentes),
                    "reservas_ativas": len(limite.reservas),
                }
            )
        )


def _escrita_do_legado(
    atual: Limite, atualizacao: AtualizacaoDoLegado, resultado: ResultadoDaSincronizacao
) -> dict[str, Any]:
    """UpdateItem condicional restrito aos campos do legado (ver `sincronizar`)."""
    x = _Expressao()
    versao_legado = x.nome("#vl", "versao_legado")
    condicoes = [f"{versao_legado} = {x.valor(':vl_atual', atual.versao_legado)}"]
    sets = [f"{versao_legado} = {x.valor(':vl', atualizacao.versao_legado)}"]
    if resultado is ResultadoDaSincronizacao.ATUALIZADO:
        concedido = x.nome("#c", "concedido")
        status = x.nome("#status", "status")
        disponivel = x.nome("#disp", "disponivel")
        versao = x.nome("#versao", "versao")
        condicoes += [
            f"{concedido} = {x.valor(':c_atual', atual.concedido)}",
            f"{status} = {x.valor(':st_atual', atual.status.value)}",
        ]
        sets += [
            f"{concedido} = {x.valor(':c', atualizacao.concedido)}",
            f"{disponivel} = {disponivel} + {x.valor(':delta', atualizacao.concedido - atual.concedido)}",
            f"{status} = {x.valor(':st', atualizacao.status.value)}",
            f"{versao} = {versao} + {x.valor(':um', 1)}",
        ]
    return {
        "UpdateExpression": "SET " + ", ".join(sets),
        "ConditionExpression": " AND ".join(condicoes),
        "ExpressionAttributeNames": x.nomes,
        "ExpressionAttributeValues": x.valores,
    }


def definicao_da_tabela(nome: str) -> dict[str, Any]:
    """Tabela `limites`: um item por contrato (PK id_contrato). Streams para CDC de LimiteAlterado.

    Fonte única para o bootstrap local e para os testes; o Terraform espelha esta definição.
    """
    return {
        "TableName": nome,
        "AttributeDefinitions": [{"AttributeName": "id_contrato", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "id_contrato", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
        "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
    }


def _dinheiro(valor: Any) -> Decimal:
    return Decimal(valor).quantize(Decimal("0.01"))


def _para_item(limite: Limite) -> dict[str, Any]:
    bruto: dict[str, object] = {
        "id_contrato": limite.id_contrato,
        "id_conta": limite.id_conta,
        "tipo_produto": limite.tipo_produto.value,
        "status": limite.status.value,
        "concedido": limite.concedido,
        "utilizado": limite.utilizado,
        "reservado": limite.reservado,
        "disponivel": limite.disponivel,
        "versao": limite.versao,
        "versao_legado": limite.versao_legado,
        "reservas": dict(limite.reservas),
        "ops": {op: int(quando.timestamp()) for op, quando in limite.operacoes_recentes.items()},
    }
    return {chave: _serializar(valor) for chave, valor in bruto.items()}


def _de_item(item: Mapping[str, Any]) -> Limite:
    d = {chave: _desserializar(valor) for chave, valor in item.items()}
    return Limite(
        id_contrato=d["id_contrato"],
        id_conta=d["id_conta"],
        tipo_produto=TipoProduto(d["tipo_produto"]),
        status=StatusContrato(d["status"]),
        concedido=_dinheiro(d["concedido"]),
        utilizado=_dinheiro(d["utilizado"]),
        reservado=_dinheiro(d["reservado"]),
        disponivel=_dinheiro(d["disponivel"]),
        versao=int(d["versao"]),
        versao_legado=int(d["versao_legado"]),
        reservas=MappingProxyType({k: _dinheiro(v) for k, v in d.get("reservas", {}).items()}),
        operacoes_recentes=MappingProxyType(
            {k: datetime.fromtimestamp(int(v), UTC) for k, v in d.get("ops", {}).items()}
        ),
    )
