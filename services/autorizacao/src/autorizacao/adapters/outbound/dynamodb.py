"""Repositório de autorizações no DynamoDB + transactional outbox.

Tabelas:
  - `autorizacoes`: um item `AUT#<id>` por autorização (o id é determinístico: é também o registro
    de idempotência). TTL de retenção quente (30 dias). A trilha de 10 anos é desenho (ADR-0017); no
    sandbox o durável é o archive de 30 dias do barramento.
  - `autorizacoes-outbox`: um item `OUTBOX#<id_evento>` por evento pendente. O relay publica no
    EventBridge e APAGA o item; por isso a tabela fica pequena e o sweeper pode varrê-la barato. O item NÃO
    tem TTL: um evento não publicado sai do outbox publicado, nunca por idade.

GSI esparso `gsi_orfas` (ADR-0006): só itens EM_PROCESSAMENTO carregam `orfa_shard` (EP#0..EP#N-1, hash do id)
e `orfa_lease_ms`; concluir substitui o item (Put) e os atributos somem, tirando-o do índice. O reconciliador
consulta os N shards por lease vencido, sem Scan na tabela quente. O índice é eventualmente consistente: o
CAS do `assumir` é quem decide, então uma entrada atrasada no índice é inofensiva.

A fila humana usa o MESMO índice com outro prefixo: RECONCILIACAO_MANUAL (e reserva vencida sem hold) leva
`orfa_shard` RM#0..RM#N-1 e `orfa_lease_ms` = quando foi encaminhada. O reconciliador só consulta EP#; o runbook
consulta RM#. Nada que precise de gente some de todos os índices.

GSI esparso `gsi_vencimentos`, mesmo desenho: só RESERVAs APROVADAS ainda pendentes carregam `venc_shard`
(RV#0..RV#N-1) e `venc_ms` (expira_em). Capturar, estornar ou expirar regrava a original com REMOVE desses
atributos e ela sai do índice. O expirador consulta os N shards por vencimento; quem decide é o CAS
"ainda APROVADA" da escrita final (ou reserva sem hold na fila humana, que o vencedor ainda encerra: sai da fila
e volta a ter TTL).

Quantos shards (N, ADR-0016 e docs/capacidade.md): a sort key dos dois índices cresce com o tempo (lease, vencimento),
então o DynamoDB não divide uma chave quente por faixa de sort key, e cada valor de `*_shard` fica preso ao teto de
1.000 WCU/s de uma partição. Toda autorização escreve 2 vezes no `gsi_orfas` (entra ao registrar, sai ao concluir):
a 5.000 TPS são ~10.000 escritas/s no índice. Com N = 10 isso dava ~1.000 WCU/s por chave, 100% do teto; com o
padrão N = 64, ~156 WCU/s por chave (~16%). N vem da configuração (SHARDS_DE_ORFAS, SHARDS_DE_VENCIMENTOS).
SUBIR N é retrocompatível: o leitor consulta 0..N-1, que contém os shards antigos, e o item muda de shard na
próxima escrita. Leitor (reconciliador) e escritor (autorizacao) devem subir juntos, ou o leitor antes: um
escritor à frente só atrasa a leitura dos shards novos. REDUZIR N esconde os itens dos shards >= N: não fazer
sem migrar.

Aqui TransactWriteItems é a escolha certa (diferente do item quente de Limite): os itens tocados são
desta autorização (e da original, no estorno) — não há contenção — e o estado final precisa sair
junto com os eventos (sem dual-write). `ClientRequestToken` torna o commit incerto idempotente (10 min).

Falhas transitórias (estrangulamento, timeout, conflito com outra transação), depois dos retries do SDK, viram
`ArmazenamentoIndisponivel` → 503 com Retry-After: repetir com a mesma Idempotency-Key é seguro. O cancelamento
de uma transação é classificado por (índice do item, código), nunca pela posição de um único motivo.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

from autorizacao.adapters.outbound.cloudevents import nome_curto, para_cloudevent, serializar
from autorizacao.application.portas import (
    ArmazenamentoIndisponivel,
    LeasePerdido,
    OriginalJaEncerrada,
)
from autorizacao.domain.autorizacao import (
    EM_CURSO,
    Autorizacao,
    MotivoDaReconciliacao,
    MotivoRecusa,
    Resultado,
    StatusAutorizacao,
)
from autorizacao.domain.eventos import EventoDeDominio, SaldosDoLimite
from autorizacao.domain.pedido import TipoOperacao

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_log = logging.getLogger(__name__)

INDICE_DE_ORFAS = "gsi_orfas"
INDICE_DE_VENCIMENTOS = "gsi_vencimentos"
# Padrão de N dos índices esparsos (conta no docstring do módulo); a configuração pode subir, nunca reduzir.
SHARDS_DE_ORFAS = (
    64  # distribui a partição do índice esparso (todas as órfãs cairiam numa só chave)
)
SHARDS_DE_VENCIMENTOS = 64
RETENCAO_QUENTE = timedelta(days=30)
# Falhas em que o SDK já esgotou os retries e repetir mais tarde é seguro → 503, não 500 (como no `limites`).
_CODIGOS_TRANSITORIOS = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
        "TransactionConflictException",  # escrita simples num item que está numa transação em curso
        "TransactionInProgressException",  # mesmo ClientRequestToken ainda em execução
    }
)
# Motivos de cancelamento de TransactWriteItems que não dizem nada sobre o estado: outra transação no mesmo
# item ou estrangulamento. Repetir é seguro (o token torna o mesmo pedido idempotente).
_CANCELAMENTOS_TRANSITORIOS = frozenset(
    {"TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded"}
)
_serializar = TypeSerializer().serialize
_desserializar = TypeDeserializer().deserialize


def _shard(id_autorizacao: str, shards: int) -> int:
    """sha256(id) % shards (estável entre processos; `hash()` do Python não é)."""
    return int(hashlib.sha256(id_autorizacao.encode()).hexdigest(), 16) % shards


def shard_de_orfas(id_autorizacao: str, shards: int = SHARDS_DE_ORFAS) -> str:
    """`EP#<n>`, n = sha256(id) % shards."""
    return f"EP#{_shard(id_autorizacao, shards)}"


@dataclass(frozen=True, slots=True)
class _IndiceEsparso:
    nome: str
    atributo_shard: str
    atributo_prazo: str
    prefixo: str
    shards: int


def _indices(
    shards_de_orfas: int, shards_de_vencimentos: int
) -> tuple[_IndiceEsparso, _IndiceEsparso]:
    if shards_de_orfas < 1 or shards_de_vencimentos < 1:
        raise ValueError("o número de shards dos índices esparsos deve ser >= 1")
    return (
        _IndiceEsparso(INDICE_DE_ORFAS, "orfa_shard", "orfa_lease_ms", "EP", shards_de_orfas),
        _IndiceEsparso(INDICE_DE_VENCIMENTOS, "venc_shard", "venc_ms", "RV", shards_de_vencimentos),
    )


# "Ainda em curso" na condição: EM_PROCESSAMENTO (1ª execução/retomada) ou COMPENSANDO (reconciliador).
_EM_CURSO = "#status IN (:em_proc, :compensando)"
_VALORES_EM_CURSO = {
    ":em_proc": {"S": StatusAutorizacao.EM_PROCESSAMENTO.value},
    ":compensando": {"S": StatusAutorizacao.COMPENSANDO.value},
}


# Reserva que o expirador pôs na fila humana por falta de hold: o vencedor que já aplicou ainda a encerra
# (espelha `Autorizacao.encerravel_pelo_vencedor`).
_NA_FILA_SEM_HOLD = "(#status = :manual AND motivo_da_reconciliacao = :sem_hold)"
_VALORES_DA_FILA_SEM_HOLD = {
    ":manual": {"S": StatusAutorizacao.RECONCILIACAO_MANUAL.value},
    ":sem_hold": {"S": MotivoDaReconciliacao.RESERVA_SEM_HOLD.value},
}


def shard_da_fila_humana(id_autorizacao: str, shards: int = SHARDS_DE_ORFAS) -> str:
    """`RM#<n>`: fila de reconciliação manual, no mesmo GSI das órfãs (o reconciliador só lê EP#)."""
    return f"RM#{_shard(id_autorizacao, shards)}"


def shard_de_vencimentos(id_autorizacao: str, shards: int = SHARDS_DE_VENCIMENTOS) -> str:
    """`RV#<n>`: espalha as reservas pendentes do dia (todas teriam vencimento próximo) em N partições."""
    return f"RV#{_shard(id_autorizacao, shards)}"


def chave(id_autorizacao: str) -> dict[str, Any]:
    return {"pk": {"S": f"AUT#{id_autorizacao}"}}


def _falhas_transitorias[**P, T](metodo: Callable[P, T]) -> Callable[P, T]:
    """Estrangulamento, timeout e conflito que escapam do método viram `ArmazenamentoIndisponivel`; o resto
    (inclusive o que o próprio método já classificou) passa intacto."""

    @functools.wraps(metodo)
    def envolvido(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return metodo(*args, **kwargs)
        except ClientError as erro:
            if erro.response.get("Error", {}).get("Code") in _CODIGOS_TRANSITORIOS:
                raise ArmazenamentoIndisponivel(metodo.__name__) from erro
            raise
        except (
            ConnectTimeoutError,
            ReadTimeoutError,
            EndpointConnectionError,
            ConnectionClosedError,
        ) as erro:
            raise ArmazenamentoIndisponivel(metodo.__name__) from erro

    return envolvido


def _cancelamento(
    erro: ClientError, alvo: str, condicoes: Mapping[int, Exception]
) -> Exception | None:
    """Classifica um TransactionCanceledException por (índice do item, código).

    `condicoes` liga o índice de um item à exceção de domínio da condição dele (na ordem de prioridade).
    ConditionalCheckFailed num desses itens → a exceção dele; conflito ou estrangulamento em qualquer item →
    `ArmazenamentoIndisponivel`.

    ConditionalCheckFailed em item FORA de `condicoes` só pode ser do outbox (`attribute_not_exists(pk)`): id de
    evento repetido, com as condições de estado todas atendidas. A transação foi cancelada inteira (nada gravado)
    e a próxima tentativa gera ids de evento novos, então repetir com a mesma Idempotency-Key resolve:
    `ArmazenamentoIndisponivel` (503 com Retry-After), com uma linha de erro, porque com ids uuid4 isso é anomalia.
    O resto (ex.: ValidationError) → None, e o chamador relança o erro original: é defeito, não estado a tratar.
    """
    resposta = cast("Mapping[str, Any]", erro.response)
    motivos = [str(m.get("Code") or "None") for m in resposta.get("CancellationReasons", [])]
    for indice, excecao in condicoes.items():
        if indice < len(motivos) and motivos[indice] == "ConditionalCheckFailed":
            return excecao
    if _CANCELAMENTOS_TRANSITORIOS.intersection(motivos):
        return ArmazenamentoIndisponivel(alvo)
    if "ConditionalCheckFailed" in motivos:
        _log.error(json.dumps({"evento": "id_de_evento_repetido_no_outbox", "alvo": alvo}))
        return ArmazenamentoIndisponivel(alvo)
    return None


class RepositorioDeAutorizacoesDynamoDB:
    def __init__(
        self,
        cliente: DynamoDBClient,
        tabela: str,
        tabela_outbox: str,
        *,
        shards_de_orfas: int = SHARDS_DE_ORFAS,
        shards_de_vencimentos: int = SHARDS_DE_VENCIMENTOS,
    ) -> None:
        self._cliente = cliente
        self._tabela = tabela
        self._outbox = tabela_outbox
        self._orfas, self._vencimentos = _indices(shards_de_orfas, shards_de_vencimentos)

    @_falhas_transitorias
    def obter(self, id_autorizacao: str) -> Autorizacao | None:
        item = self._cliente.get_item(
            TableName=self._tabela, Key=chave(id_autorizacao), ConsistentRead=True
        ).get("Item")
        return _de_item(item) if item else None

    @_falhas_transitorias
    def registrar(self, autorizacao: Autorizacao) -> Autorizacao | None:
        try:
            self._cliente.put_item(
                TableName=self._tabela,
                Item=self._item(autorizacao),
                ConditionExpression="attribute_not_exists(pk)",
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return None
        except self._cliente.exceptions.ConditionalCheckFailedException as erro:
            item = cast("Mapping[str, Any] | None", erro.response.get("Item"))
            existente = _de_item(item) if item else self.obter(autorizacao.id_autorizacao)
            if existente is None:  # pragma: no cover - só se alguém apagar no meio
                raise
            return existente

    @_falhas_transitorias
    def assumir(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        try:
            self._cliente.update_item(
                TableName=self._tabela,
                Key=chave(autorizacao.id_autorizacao),
                # orfa_lease_ms acompanha o lease: a assumida só reaparece no índice quando vencer de novo.
                UpdateExpression=(
                    "SET lease_ate_ms = :novo, orfa_lease_ms = :novo, orfa_shard = :shard, "
                    "atualizada_em = :agora"
                ),
                ConditionExpression=f"{_EM_CURSO} AND lease_ate_ms = :anterior",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":novo": {"N": str(_ms(autorizacao.lease_ate))},
                    ":shard": {"S": shard_de_orfas(autorizacao.id_autorizacao, self._orfas.shards)},
                    ":agora": {"S": autorizacao.atualizada_em.isoformat()},
                    **_VALORES_EM_CURSO,
                    ":anterior": {"N": str(_ms(lease_anterior))},
                },
            )
            return True
        except self._cliente.exceptions.ConditionalCheckFailedException:
            return False

    @_falhas_transitorias
    def regravar(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        try:
            self._cliente.put_item(
                TableName=self._tabela,
                Item=self._item(autorizacao),
                ConditionExpression=f"{_EM_CURSO} AND lease_ate_ms = :anterior",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    **_VALORES_EM_CURSO,
                    ":anterior": {"N": str(_ms(lease_anterior))},
                },
            )
            return True
        except self._cliente.exceptions.ConditionalCheckFailedException:
            return False

    @_falhas_transitorias
    def encaminhar_reserva(self, encaminhada: Autorizacao) -> None:
        assert encaminhada.motivo_da_reconciliacao is not None  # garantido pelo agregado
        itens: list[Any] = [
            {
                "Update": {
                    "TableName": self._tabela,
                    "Key": chave(encaminhada.id_autorizacao),
                    # Sai do índice de vencimentos, entra na fila humana e perde o TTL (fica até ser resolvida).
                    "UpdateExpression": (
                        "SET #status = :manual, motivo_da_reconciliacao = :m, orfa_shard = :fila, "
                        "orfa_lease_ms = :agora_ms, atualizada_em = :agora REMOVE venc_shard, venc_ms, #ttl"
                    ),
                    "ConditionExpression": "#status = :aprovada",
                    "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl"},
                    "ExpressionAttributeValues": {
                        ":manual": {"S": StatusAutorizacao.RECONCILIACAO_MANUAL.value},
                        ":m": {"S": encaminhada.motivo_da_reconciliacao.value},
                        ":fila": {
                            "S": shard_da_fila_humana(
                                encaminhada.id_autorizacao, self._orfas.shards
                            )
                        },
                        ":agora_ms": {"N": str(_ms(encaminhada.atualizada_em))},
                        ":agora": {"S": encaminhada.atualizada_em.isoformat()},
                        ":aprovada": {"S": StatusAutorizacao.APROVADA.value},
                    },
                }
            },
            *(self._put_no_outbox(evento) for evento in encaminhada.eventos),
        ]
        try:
            self._transacionar(itens)
        except self._cliente.exceptions.TransactionCanceledException as erro:
            # Item 0: a reserva (ainda APROVADA?). Os demais: outbox.
            classificada = _cancelamento(
                erro,
                encaminhada.id_autorizacao,
                {0: OriginalJaEncerrada(encaminhada.id_autorizacao)},
            )
            if classificada is None:
                raise
            raise classificada from erro

    @_falhas_transitorias
    def marcar_estorno(
        self, id_original: str, id_estorno: str, substituir: str | None = None
    ) -> str | None:
        livre = "attribute_not_exists(estorno_em_curso) OR estorno_em_curso = :e"
        valores = {
            ":e": {"S": id_estorno},
            ":aprovada": {"S": StatusAutorizacao.APROVADA.value},
            **_VALORES_DA_FILA_SEM_HOLD,
        }
        if substituir is not None:
            livre += " OR estorno_em_curso = :velho"
            valores[":velho"] = {"S": substituir}
        try:
            self._cliente.update_item(
                TableName=self._tabela,
                Key=chave(id_original),
                UpdateExpression="SET estorno_em_curso = :e",
                # Reserva na fila humana por falta de hold: só a retomada do estorno que já tinha a marca segue.
                ConditionExpression=(
                    f"(#status = :aprovada AND ({livre})) OR "
                    f"({_NA_FILA_SEM_HOLD} AND estorno_em_curso = :e)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=valores,
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return None
        except self._cliente.exceptions.ConditionalCheckFailedException as erro:
            antigo = cast("Mapping[str, Any]", erro.response.get("Item") or {})
            if antigo.get("status", {}).get("S") != StatusAutorizacao.APROVADA.value:
                return ""
            return str(antigo.get("estorno_em_curso", {}).get("S", ""))

    @_falhas_transitorias
    def listar_orfas(self, lease_vencido_antes_de: datetime, limite: int) -> list[Autorizacao]:
        """EM_PROCESSAMENTO com lease vencido antes do instante dado, mais antigas primeiro.

        Query nos N shards do GSI esparso (cada um já ordenado por lease); junta, ordena e corta.
        Leitura eventualmente consistente (GSI): quem decide é o CAS de `assumir`.
        """
        orfas = self._consultar_shards(self._orfas, lease_vencido_antes_de, limite)
        return sorted(orfas, key=lambda a: a.lease_ate)[:limite]

    @_falhas_transitorias
    def listar_reservas_vencidas(
        self, vencidas_antes_de: datetime, limite: int
    ) -> list[Autorizacao]:
        """RESERVAs APROVADAS com validade terminada, mais antigas primeiro (GSI esparso de vencimentos)."""
        vencidas = self._consultar_shards(self._vencimentos, vencidas_antes_de, limite)
        return sorted(vencidas, key=lambda a: a.criada_em)[:limite]

    def _consultar_shards(
        self, indice: _IndiceEsparso, antes_de: datetime, limite: int
    ) -> list[Autorizacao]:
        """Query em cada shard do índice esparso (já ordenado pelo prazo), até `limite` por shard."""
        encontradas: list[Autorizacao] = []
        for n in range(indice.shards):
            argumentos: dict[str, Any] = {
                "TableName": self._tabela,
                "IndexName": indice.nome,
                "KeyConditionExpression": f"{indice.atributo_shard} = :s AND {indice.atributo_prazo} < :t",
                "ExpressionAttributeValues": {
                    ":s": {"S": f"{indice.prefixo}#{n}"},
                    ":t": {"N": str(_ms(antes_de))},
                },
                "Limit": limite,
            }
            neste_shard = 0
            while neste_shard < limite:
                pagina = self._cliente.query(**argumentos)
                itens = pagina.get("Items", [])
                encontradas.extend(_de_item(item) for item in itens)
                neste_shard += len(itens)
                ultima = pagina.get("LastEvaluatedKey")
                if not ultima:
                    break
                argumentos["ExclusiveStartKey"] = ultima
        return encontradas

    @_falhas_transitorias
    def registrar_expiracao(self, expirada: Autorizacao) -> None:
        itens: list[Any] = [
            _encerrar_original(self._tabela, expirada),
            *(self._put_no_outbox(evento) for evento in expirada.eventos),
        ]
        try:
            self._transacionar(itens)
        except self._cliente.exceptions.TransactionCanceledException as erro:
            # Item 0: a reserva (ainda APROVADA?). Os demais: outbox.
            classificada = _cancelamento(
                erro, expirada.id_autorizacao, {0: OriginalJaEncerrada(expirada.id_autorizacao)}
            )
            if classificada is None:
                raise
            raise classificada from erro

    def _item(self, autorizacao: Autorizacao) -> dict[str, Any]:
        return _para_item(autorizacao, self._orfas.shards, self._vencimentos.shards)

    def _put_no_outbox(self, evento: EventoDeDominio) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._outbox,
                "Item": _item_do_outbox(evento),
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    def _transacionar(self, itens: list[Any]) -> None:
        # Token = hash do conteúdo INTEIRO da transação: o escopo do token é a conta (não a tabela),
        # então só parâmetros idênticos podem compartilhá-lo. O retry do SDK (mesmos parâmetros)
        # continua idempotente; pedidos diferentes nunca colidem (IdempotentParameterMismatch).
        token = hashlib.sha256(json.dumps(itens, sort_keys=True).encode()).hexdigest()[:36]
        self._cliente.transact_write_items(TransactItems=itens, ClientRequestToken=token)

    @_falhas_transitorias
    def concluir(self, autorizacao: Autorizacao, original_encerrada: Autorizacao | None) -> None:
        itens: list[Any] = [
            {
                "Put": {
                    "TableName": self._tabela,
                    "Item": self._item(autorizacao),
                    "ConditionExpression": f"{_EM_CURSO} AND lease_ate_ms = :lease",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        **_VALORES_EM_CURSO,
                        ":lease": {"N": str(_ms(autorizacao.lease_ate))},
                    },
                }
            }
        ]
        if original_encerrada is not None:
            itens.append(_encerrar_original(self._tabela, original_encerrada))
        itens.extend(self._put_no_outbox(evento) for evento in autorizacao.eventos)
        try:
            self._transacionar(itens)
        except self._cliente.exceptions.TransactionCanceledException as erro:
            # Item 0: esta autorização (dono do lease). Item 1, se houver original: ela (ainda APROVADA?).
            # Os demais: outbox.
            condicoes: dict[int, Exception] = {0: LeasePerdido(autorizacao.id_autorizacao)}
            if original_encerrada is not None:
                condicoes[1] = OriginalJaEncerrada(original_encerrada.id_autorizacao)
            classificada = _cancelamento(erro, autorizacao.id_autorizacao, condicoes)
            if classificada is None:
                raise
            raise classificada from erro


def _encerrar_original(tabela: str, original: Autorizacao) -> dict[str, Any]:
    """→ ESTORNADA/CAPTURADA/EXPIRADA, só se a gravada ainda pode ser encerrada pelo vencedor
    (`Autorizacao.encerravel_pelo_vencedor`: APROVADA, ou reserva sem hold na fila humana). Sai do índice de
    vencimentos e da fila humana, perde o motivo da fila e volta a ter TTL (em APROVADA, os REMOVE não acham nada
    e o TTL regravado é o mesmo)."""
    return {
        "Update": {
            "TableName": tabela,
            "Key": chave(original.id_autorizacao),
            "UpdateExpression": (
                "SET #status = :novo, atualizada_em = :agora, #ttl = :ttl "
                "REMOVE venc_shard, venc_ms, orfa_shard, orfa_lease_ms, motivo_da_reconciliacao"
            ),
            "ConditionExpression": f"#status = :aprovada OR {_NA_FILA_SEM_HOLD}",
            "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl"},
            "ExpressionAttributeValues": {
                ":novo": {"S": original.status.value},
                ":aprovada": {"S": StatusAutorizacao.APROVADA.value},
                ":agora": {"S": original.atualizada_em.isoformat()},
                ":ttl": {"N": str(_ttl_quente(original))},
                **_VALORES_DA_FILA_SEM_HOLD,
            },
        }
    }


def definicoes_das_tabelas(tabela: str, tabela_outbox: str) -> list[dict[str, Any]]:
    """Fonte única para bootstrap local e testes; o Terraform espelha (TTL é ligado à parte)."""
    atributo_pk = {"AttributeName": "pk", "AttributeType": "S"}
    base = {
        "AttributeDefinitions": [atributo_pk],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
    }
    return [
        {
            "TableName": tabela,
            **base,
            "AttributeDefinitions": [
                atributo_pk,
                {"AttributeName": "orfa_shard", "AttributeType": "S"},
                {"AttributeName": "orfa_lease_ms", "AttributeType": "N"},
                {"AttributeName": "venc_shard", "AttributeType": "S"},
                {"AttributeName": "venc_ms", "AttributeType": "N"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": INDICE_DE_ORFAS,
                    "KeySchema": [
                        {"AttributeName": "orfa_shard", "KeyType": "HASH"},
                        {"AttributeName": "orfa_lease_ms", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": INDICE_DE_VENCIMENTOS,
                    "KeySchema": [
                        {"AttributeName": "venc_shard", "KeyType": "HASH"},
                        {"AttributeName": "venc_ms", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        },
        {
            "TableName": tabela_outbox,
            **base,
            "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
        },
    ]


# --- mapeamento item ↔ agregado ---------------------------------------------------------------


def _ms(instante: datetime) -> int:
    return int(instante.timestamp() * 1000)


def _ttl_quente(a: Autorizacao) -> int:
    """Fim da retenção quente, em epoch de segundos (o formato do TTL do DynamoDB)."""
    return int((a.criada_em + RETENCAO_QUENTE).timestamp())


def _item_do_outbox(evento: EventoDeDominio) -> dict[str, Any]:
    bruto: dict[str, object] = {
        "pk": f"OUTBOX#{evento.id_evento}",
        "id_evento": evento.id_evento,
        "detail_type": nome_curto(evento),
        "cloudevent": serializar(para_cloudevent(evento)),
        "id_contrato": evento.id_contrato,
        "criado_em_ms": _ms(evento.ocorrido_em),
    }
    return {k: _serializar(v) for k, v in bruto.items()}


def _para_item(
    a: Autorizacao,
    shards_de_orfas: int = SHARDS_DE_ORFAS,
    shards_de_vencimentos: int = SHARDS_DE_VENCIMENTOS,
) -> dict[str, Any]:
    em_curso = a.status in EM_CURSO
    na_fila_humana = a.status is StatusAutorizacao.RECONCILIACAO_MANUAL
    bruto: dict[str, object | None] = {
        "pk": f"AUT#{a.id_autorizacao}",
        "id_autorizacao": a.id_autorizacao,
        "id_contrato": a.id_contrato,
        "id_conta": a.id_conta,
        "valor": a.valor,
        "moeda": a.moeda,
        "tipo": a.tipo.value,
        "id_estabelecimento": a.id_estabelecimento,
        "id_autorizacao_original": a.id_autorizacao_original,
        "metadata": dict(a.metadata) or None,
        "impressao_digital": a.impressao_digital,
        "client_id": a.client_id,
        "status": a.status.value,
        "resultado": a.resultado.value if a.resultado else None,
        "motivo_recusa": a.motivo_recusa.value if a.motivo_recusa else None,
        "saldos": _saldos_para_mapa(a.saldos),
        "tipo_operacao_original": a.tipo_operacao_original.value
        if a.tipo_operacao_original
        else None,
        "criada_em": a.criada_em.isoformat(),
        "atualizada_em": a.atualizada_em.isoformat(),
        "lease_ate_ms": _ms(a.lease_ate),
        # Em curso → índice de órfãs (EP#, pelo lease); reconciliação manual → fila humana (RM#, pela data);
        # qualquer outro estado → fora do índice (os atributos somem no Put).
        "orfa_shard": shard_de_orfas(a.id_autorizacao, shards_de_orfas)
        if em_curso
        else shard_da_fila_humana(a.id_autorizacao, shards_de_orfas)
        if na_fila_humana
        else None,
        "orfa_lease_ms": _ms(a.lease_ate)
        if em_curso
        else _ms(a.atualizada_em)
        if na_fila_humana
        else None,
        "aplicacao_confirmada_em": a.aplicacao_confirmada_em.isoformat()
        if a.aplicacao_confirmada_em
        else None,
        "desfazer_desde": a.desfazer_desde.isoformat() if a.desfazer_desde else None,
        "motivo_da_reconciliacao": a.motivo_da_reconciliacao.value
        if a.motivo_da_reconciliacao
        else None,
        # Só RESERVA aprovada e pendente entra no GSI esparso de vencimentos.
        "venc_shard": shard_de_vencimentos(a.id_autorizacao, shards_de_vencimentos)
        if a.reserva_pendente
        else None,
        "venc_ms": _ms(a.expira_em) if a.reserva_pendente and a.expira_em else None,
        "correlation_id": a.correlation_id,
        "traceparent": a.traceparent,
        # Na fila humana não há TTL: o caso fica até alguém resolver (o evento vai para o archive do barramento).
        "ttl": None if na_fila_humana else _ttl_quente(a),
    }
    return {k: _serializar(v) for k, v in bruto.items() if v is not None}


def _saldos_para_mapa(s: SaldosDoLimite | None) -> dict[str, object] | None:
    if s is None:
        return None
    return {
        "limite_concedido": s.limite_concedido,
        "limite_utilizado": s.limite_utilizado,
        "saldo_reservado": s.saldo_reservado,
        "limite_disponivel": s.limite_disponivel,
        "versao": s.versao,
    }


def _dinheiro(valor: Any) -> Decimal:
    return Decimal(valor).quantize(Decimal("0.01"))


def _de_item(item: Mapping[str, Any]) -> Autorizacao:
    d = {k: _desserializar(v) for k, v in item.items()}
    saldos = d.get("saldos")
    return Autorizacao(
        id_autorizacao=d["id_autorizacao"],
        id_contrato=d["id_contrato"],
        id_conta=d["id_conta"],
        valor=_dinheiro(d["valor"]),
        moeda=d["moeda"],
        tipo=TipoOperacao(d["tipo"]),
        id_estabelecimento=d.get("id_estabelecimento"),
        id_autorizacao_original=d.get("id_autorizacao_original"),
        metadata=MappingProxyType(d.get("metadata") or {}),
        impressao_digital=d["impressao_digital"],
        client_id=d["client_id"],
        status=StatusAutorizacao(d["status"]),
        criada_em=datetime.fromisoformat(d["criada_em"]),
        atualizada_em=datetime.fromisoformat(d["atualizada_em"]),
        lease_ate=datetime.fromtimestamp(int(d["lease_ate_ms"]) / 1000, UTC),
        correlation_id=d["correlation_id"],
        traceparent=d.get("traceparent"),
        resultado=Resultado(d["resultado"]) if d.get("resultado") else None,
        motivo_recusa=MotivoRecusa(d["motivo_recusa"]) if d.get("motivo_recusa") else None,
        saldos=SaldosDoLimite(
            limite_concedido=_dinheiro(saldos["limite_concedido"]),
            limite_utilizado=_dinheiro(saldos["limite_utilizado"]),
            saldo_reservado=_dinheiro(saldos["saldo_reservado"]),
            limite_disponivel=_dinheiro(saldos["limite_disponivel"]),
            versao=int(saldos["versao"]),
        )
        if saldos
        else None,
        tipo_operacao_original=TipoOperacao(d["tipo_operacao_original"])
        if d.get("tipo_operacao_original")
        else None,
        aplicacao_confirmada_em=_instante(d.get("aplicacao_confirmada_em")),
        desfazer_desde=_instante(d.get("desfazer_desde")),
        motivo_da_reconciliacao=MotivoDaReconciliacao(d["motivo_da_reconciliacao"])
        if d.get("motivo_da_reconciliacao")
        else None,
    )


def _instante(valor: Any) -> datetime | None:
    return datetime.fromisoformat(valor) if valor else None
