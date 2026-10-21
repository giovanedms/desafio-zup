"""Núcleo do consumidor de referência: mensagem → CloudEvent validado → reivindicação na inbox → linha a jusante.

A entrega é at-least-once e sem ordem: o EventBridge entrega em fila SQS Standard, e relay e sweeper podem publicar o
mesmo evento duas vezes (ADR-0007, ADR-0009). Daí as três regras:

- **Idempotente por (source, id) do CloudEvent.** O efeito (a linha que o sistema a jusante recebe) não cabe numa
  escrita atômica com a inbox, como o lançamento da contabilização cabe. Então a inbox REIVINDICA o evento antes do
  efeito, com prazo (o fim da invocação), e o CONCLUI depois. Reentrega de evento concluído é duplicata. Reentrega
  concorrente encontra a reivindicação viva e volta para a fila. Invocação que morre entre o efeito e a conclusão
  deixa uma reivindicação que vence no prazo, e a reentrega emite de novo (at-least-once; a linha leva o `id`).
- **Tolera reordenação.** Cada evento é um fato autocontido: nenhum é descartado nem falha por chegar antes ou
  depois de outro do mesmo contrato. A linha leva `ocorrido_em` e, quando o evento tem, `versao_limite`, para o
  destino que precisar ordenar.
- **Não descarta o que não entende.** Mensagem fora do envelope ou do schema do tipo levanta `EventoInvalido` e volta
  como falha do lote: o SQS reentrega e, esgotados os recebimentos, move para a DLQ da fila (que tem alarme).

Sem AWS SDK nem biblioteca de JSON Schema aqui: o validador e a inbox entram por porta.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

# O que vai para a linha, além de tipo, id, source e instante: identificadores pseudonimizados e a versão do limite
# (ordenação). Valor, conta, estabelecimento, canal e motivo ficam de fora (minimização, docs/compliance.md): o destino
# que precisar deles consulta pelo `id_autorizacao`.
CAMPOS_DE_DATA_NA_LINHA = (
    "id_contrato",
    "id_autorizacao",
    "id_autorizacao_original",
    "versao_limite",
    "correlation_id",
)


class EventoInvalido(ValueError):
    """Mensagem que não cumpre o envelope do EventBridge, o envelope CloudEvents ou o schema do tipo.

    Cada erro diz ONDE e QUAL regra (caminho JSON e palavra-chave do schema), nunca o valor recebido: o valor pode ser
    dado pessoal ou financeiro, e o erro vai para o log.
    """

    def __init__(self, *erros: str) -> None:
        super().__init__("; ".join(erros))
        self.erros = erros


class Situacao(StrEnum):
    """Resposta da inbox à reivindicação de um evento."""

    NOVO = "NOVO"  # esta invocação ficou com o evento: emite e conclui
    CONCLUIDO = "CONCLUIDO"  # já emitido: duplicata
    EM_ANDAMENTO = "EM_ANDAMENTO"  # outra invocação está com ele, dentro do prazo


class Desfecho(StrEnum):
    CONSUMIDO = "CONSUMIDO"
    DUPLICADO = "DUPLICADO"
    EM_ANDAMENTO = (
        "EM_ANDAMENTO"  # volta para a fila; na reentrega a outra invocação já terá concluído
    )


class Inbox(Protocol):
    def reivindicar(self, chave: str, agora_ms: int, prazo_ms: int) -> Situacao:
        """Grava a reivindicação (válida até `prazo_ms`) se a chave não existe ou se a reivindicação anterior venceu
        sem conclusão (prazo < `agora_ms`). Senão, diz se o evento já foi concluído ou ainda está em andamento."""
        ...

    def concluir(self, chave: str) -> None:
        """Marca como concluída a chave que esta invocação reivindicou."""
        ...


class Validador(Protocol):
    def validar(self, cloudevent: Mapping[str, Any]) -> None:
        """Levanta EventoInvalido se o CloudEvent não cumpre o schema do seu `type`."""
        ...


def cloudevent_da_mensagem(corpo: str, validador: Validador) -> Mapping[str, Any]:
    """Corpo da mensagem SQS (envelope do EventBridge: `detail-type`, `detail`) → CloudEvent validado.

    `parse_float=Decimal`: número do texto sem passar por float. O `detail-type`, pelo qual a regra roteou, tem de ser
    o `data.event_type` do CloudEvent: divergência é defeito do produtor e não pode passar como outro tipo.
    """
    try:
        envelope = json.loads(corpo, parse_float=Decimal)
    except (TypeError, ValueError) as erro:
        raise EventoInvalido(f"$: corpo da mensagem não é JSON ({type(erro).__name__})") from erro
    detail = envelope.get("detail") if isinstance(envelope, dict) else None
    if not isinstance(detail, dict):
        raise EventoInvalido("$: envelope do EventBridge sem 'detail' objeto")
    validador.validar(detail)
    data = detail.get("data")
    if not isinstance(data, dict) or envelope.get("detail-type") != data.get("event_type"):
        raise EventoInvalido("$.detail-type: diverge de data.event_type")
    return detail


def linha_a_jusante(consumidor: str, cloudevent: Mapping[str, Any]) -> dict[str, Any]:
    """O registro que o sistema a jusante recebe deste evento (uma linha estruturada por evento consumido)."""
    data = cloudevent["data"]
    return {
        "evento": "evento_consumido",
        "consumidor": consumidor,
        "tipo": cloudevent["type"],
        "id": cloudevent["id"],
        "source": cloudevent["source"],
        "ocorrido_em": cloudevent["time"],
        **{campo: _texto_ou_int(data[campo]) for campo in CAMPOS_DE_DATA_NA_LINHA if campo in data},
    }


def _texto_ou_int(valor: Any) -> Any:
    """`versao_limite` que o EventBridge entregou como `42.0` (Decimal, já validado como inteiro) sai como 42."""
    return int(valor) if isinstance(valor, Decimal) else valor


@dataclass(frozen=True, slots=True)
class ConsumirEvento:
    consumidor: str
    validador: Validador
    inbox: Inbox
    emitir: Callable[[dict[str, Any]], None]
    relogio_ms: Callable[[], int]

    def executar(self, corpo: str, prazo_ms: int) -> Desfecho:
        """`prazo_ms`: até quando esta invocação pode estar viva (epoch em ms); a reivindicação vale até lá.

        Falha ao concluir (depois de emitir) levanta, e a mensagem volta como falha do lote: na reentrega, com a
        reivindicação vencida, a linha sai de novo. É o lado at-least-once; a alternativa (engolir) deixaria um
        defeito persistente na inbox, como permissão faltando, desligar a deduplicação em silêncio.
        """
        cloudevent = cloudevent_da_mensagem(corpo, self.validador)
        chave = f"{cloudevent['source']}|{cloudevent['id']}"
        situacao = self.inbox.reivindicar(chave, self.relogio_ms(), prazo_ms)
        if situacao is Situacao.CONCLUIDO:
            return Desfecho.DUPLICADO
        if situacao is Situacao.EM_ANDAMENTO:
            return Desfecho.EM_ANDAMENTO
        self.emitir(linha_a_jusante(self.consumidor, cloudevent))
        self.inbox.concluir(chave)
        return Desfecho.CONSUMIDO
