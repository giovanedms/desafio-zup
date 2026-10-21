"""Modo LOCAL: poller do DynamoDB Streams da LocalStack. `python -m relay.local`.

O núcleo (`processar`, `varrer`) é o mesmo da Lambda; só a entrada muda.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from relay.cdc_limites import (
    SOURCE_NO_BUS,
    OutboxNoop,
    RegistroMalformado,
    de_registro_do_stream_de_limites,
)
from relay.eventbridge import PublicadorEventBridge
from relay.nucleo import EventoPendente, Outbox, Publicador, ResultadoDoLote, processar
from relay.outbox_dynamodb import OutboxDynamoDB, de_registro_do_stream
from relay.sweeper import varrer

if TYPE_CHECKING:
    from mypy_boto3_dynamodbstreams.client import DynamoDBStreamsClient

log = logging.getLogger("relay.local")

# Endpoint local por allowlist exata, sem parser de URL: urlparse e o urllib3 do botocore discordam em casos como
# `http://exemplo.com\@localhost/`, que um leria como local e o outro discaria para fora. Porta de 1 a 65535;
# ASCII para o K de Kelvin não casar com `k` no IGNORECASE. A mesma regex em todo guard do repositório
# (tests/contract/test_guards_de_endpoint.py).
ENDPOINT_LOCAL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|localstack)"
    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",
    re.IGNORECASE | re.ASCII,
)
INTERVALO_S = 0.5
VARREDURA_S = 60.0
REDESCOBERTA_DE_SHARDS_S = 5.0
HEARTBEAT = Path("/tmp/relay.heartbeat")
REGIAO_PADRAO = "sa-east-1"


def cliente_local(servico: str, endpoint: str, regiao: str = REGIAO_PADRAO) -> Any:
    """Endpoint e credenciais EXPLÍCITOS; recusa endpoint que não seja local."""
    if not ENDPOINT_LOCAL.fullmatch(endpoint):
        raise ValueError(f"endpoint não é local: {endpoint!r}")
    return boto3.client(  # type: ignore[call-overload]
        servico,
        endpoint_url=endpoint,
        region_name=regiao,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(  # type: ignore[call-arg]  # os stubs do botocore não declaram o parâmetro (existe desde o 1.31)
            retries={"mode": "standard"},
            ignore_configured_endpoint_urls=True,
        ),
    )


class LeitorDeStream:
    """Lê todos os shards do Stream; tolera shards novos e fechados.

    Iterador expirado (15 min sem uso) retoma DEPOIS da última sequência lida no shard
    (AFTER_SEQUENCE_NUMBER), não do começo; só volta ao TRIM_HORIZON se nada foi lido ainda ou se os dados
    já foram aparados (TrimmedDataAccessException). Registro malformado do CDC é logado e pulado: aqui não
    há DLQ (na Lambda ele volta como falha do lote e vai para a DLQ do ESM).
    """

    def __init__(
        self,
        streams: DynamoDBStreamsClient,
        stream_arn: str,
        conversor: Callable[[Mapping[str, Any]], EventoPendente | None] = de_registro_do_stream,
    ) -> None:
        self._streams = streams
        self._arn = stream_arn
        self._conversor = conversor
        self._iteradores: dict[str, str] = {}
        self._ultima_sequencia: dict[str, str] = {}
        self._encerrados: set[str] = set()
        self._ultima_descoberta = float("-inf")

    def _descobrir_shards(self) -> None:
        argumentos: dict[str, Any] = {"StreamArn": self._arn}
        while True:
            descricao = self._streams.describe_stream(**argumentos)["StreamDescription"]
            for shard in descricao.get("Shards", []):
                id_shard = shard["ShardId"]
                if id_shard not in self._iteradores and id_shard not in self._encerrados:
                    self._iteradores[id_shard] = self._iterador_inicial(id_shard)
            ultimo = descricao.get("LastEvaluatedShardId")
            if not ultimo:
                return
            argumentos["ExclusiveStartShardId"] = ultimo

    def _iterador_inicial(self, id_shard: str) -> str:
        return self._streams.get_shard_iterator(
            StreamArn=self._arn, ShardId=id_shard, ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]

    def _iterador_de_retomada(self, id_shard: str) -> str:
        ultima = self._ultima_sequencia.get(id_shard)
        if ultima is None:
            return self._iterador_inicial(id_shard)
        try:
            return self._streams.get_shard_iterator(
                StreamArn=self._arn,
                ShardId=id_shard,
                ShardIteratorType="AFTER_SEQUENCE_NUMBER",
                SequenceNumber=ultima,
            )["ShardIterator"]
        except ClientError as erro:
            if erro.response.get("Error", {}).get("Code") != "TrimmedDataAccessException":
                raise
            # a posição já saiu da retenção: o mais antigo que há
            return self._iterador_inicial(id_shard)

    def _converter(self, id_shard: str, registro: Mapping[str, Any]) -> EventoPendente | None:
        try:
            return self._conversor(registro)
        except RegistroMalformado as erro:
            sequencia = registro.get("dynamodb", {}).get("SequenceNumber")
            log.error(
                json.dumps(
                    {
                        "evento": "cdc_registro_malformado",
                        "shard": id_shard,
                        "sequencia": sequencia,
                        "erro": str(erro),
                    }
                )
            )
            return None

    def ler(self) -> list[EventoPendente]:
        agora = time.monotonic()
        if agora - self._ultima_descoberta >= REDESCOBERTA_DE_SHARDS_S:
            self._descobrir_shards()
            self._ultima_descoberta = agora
        pendentes: list[EventoPendente] = []
        for id_shard, iterador in list(self._iteradores.items()):
            try:
                resposta = self._streams.get_records(ShardIterator=iterador)
            except ClientError as erro:
                codigo = erro.response.get("Error", {}).get("Code")
                if codigo == "ExpiredIteratorException":
                    log.warning("iterador do shard %s expirou; retomando da última lida", id_shard)
                    self._iteradores[id_shard] = self._iterador_de_retomada(id_shard)
                    continue
                if codigo == "TrimmedDataAccessException":
                    log.warning("dados do shard %s aparados; recomeçando do mais antigo", id_shard)
                    self._iteradores[id_shard] = self._iterador_inicial(id_shard)
                    continue
                raise
            for registro in resposta.get("Records", []):
                pendente = self._converter(id_shard, registro)
                if pendente is not None:
                    pendentes.append(pendente)
                sequencia = registro.get("dynamodb", {}).get("SequenceNumber")
                if sequencia:
                    self._ultima_sequencia[id_shard] = str(sequencia)
            proximo = resposta.get("NextShardIterator")
            if proximo:
                self._iteradores[id_shard] = proximo
            else:  # shard fechado e lido até o fim
                del self._iteradores[id_shard]
                self._encerrados.add(id_shard)
        return pendentes


def uma_iteracao(leitor: LeitorDeStream, publicador: Publicador, outbox: Outbox) -> ResultadoDoLote:
    pendentes = leitor.ler()
    resultado = processar(pendentes, publicador, outbox)
    if pendentes:
        log.info(
            json.dumps(
                {
                    "msg": "lote",
                    "publicados": len(resultado.publicados),
                    "falhos": len(resultado.falhos),
                }
            )
        )
    return resultado


def _descobrir_stream_arn(dynamodb: Any, tabela: str) -> str:
    arn = dynamodb.describe_table(TableName=tabela)["Table"].get("LatestStreamArn")
    if not arn:
        raise RuntimeError(f"tabela {tabela!r} sem Stream habilitado")
    return str(arn)


def _leitor_de_limites(dynamodb: Any, streams: Any, tabela: str) -> LeitorDeStream | None:
    """Stream da tabela `limites` (CDC). None enquanto a tabela ainda não existe (bootstrap pendente)."""
    try:
        arn = _descobrir_stream_arn(dynamodb, tabela)
    except (ClientError, RuntimeError):
        log.warning("tabela %s ainda sem Stream; CDC de limites adiado", tabela)
        return None
    return LeitorDeStream(streams, arn, de_registro_do_stream_de_limites)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        sys.exit("AWS_ENDPOINT_URL é obrigatório (modo local)")
    try:
        dynamodb = cliente_local("dynamodb", endpoint)
    except ValueError as erro:
        sys.exit(f"recusado: {erro}")
    tabela = os.environ.get("TABELA_OUTBOX", "autorizacoes-outbox")
    tabela_limites = os.environ.get("TABELA_LIMITES", "limites")
    bus = os.environ.get("BUS", "financas")
    streams = cliente_local("dynamodbstreams", endpoint)
    eventos = cliente_local("events", endpoint)
    publicador = PublicadorEventBridge(eventos, bus)
    publicador_limites = PublicadorEventBridge(eventos, bus, source=SOURCE_NO_BUS)
    outbox = OutboxDynamoDB(dynamodb, tabela)
    leitor = LeitorDeStream(streams, _descobrir_stream_arn(dynamodb, tabela))
    leitor_limites = _leitor_de_limites(dynamodb, streams, tabela_limites)
    log.info("relay local iniciado: tabela=%s limites=%s bus=%s", tabela, tabela_limites, bus)

    ultima_varredura = time.monotonic()
    while True:
        try:
            uma_iteracao(leitor, publicador, outbox)
            if leitor_limites is None:
                leitor_limites = _leitor_de_limites(dynamodb, streams, tabela_limites)
            if leitor_limites is not None:
                uma_iteracao(leitor_limites, publicador_limites, OutboxNoop())
            if time.monotonic() - ultima_varredura >= VARREDURA_S:
                ultima_varredura = time.monotonic()
                varredura = varrer(outbox, publicador, int(time.time() * 1000))
                if varredura.publicados or varredura.falhos:
                    log.info(
                        json.dumps(
                            {
                                "msg": "sweeper",
                                "publicados": len(varredura.publicados),
                                "falhos": len(varredura.falhos),
                            }
                        )
                    )
        except Exception:
            log.exception("iteração falhou; tentando de novo")
        HEARTBEAT.touch()
        time.sleep(INTERVALO_S)


if __name__ == "__main__":
    main()
