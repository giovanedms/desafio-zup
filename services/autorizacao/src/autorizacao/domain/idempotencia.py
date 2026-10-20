"""Idempotência: identidade determinística, impressão digital do pedido e decisão de reexecução.

- `id_autorizacao` = UUIDv5(escopo). O mesmo item no banco é o registro de idempotência E a
  autorização: a janela de idempotência é a retenção da própria autorização (30 dias quentes),
  maior que a vida de uma reserva (7 dias).
- Escopo = (client_id do token, método, URI concreta, chave). A URI concreta contém o id_contrato:
  a mesma chave em contratos diferentes NÃO colide.
- Impressão digital = SHA-256 do JSON canônico (chaves ordenadas, sem espaços — no espírito do JCS,
  RFC 8785) do conteúdo SEMÂNTICO normalizado do pedido.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from autorizacao.domain.autorizacao import Autorizacao, StatusAutorizacao
from autorizacao.domain.pedido import PedidoDeAutorizacao

NAMESPACE_AUTORIZACOES = uuid.UUID("5b0c6f0e-2a3d-4e8f-9a7b-1c2d3e4f5a6b")
NAMESPACE_ESTORNOS = uuid.UUID("9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a")
NAMESPACE_EXPIRACOES = uuid.UUID("c4b3a291-8f7e-4d6c-9b5a-0e1f2d3c4b5a")


def id_operacao_de_estorno(id_autorizacao_original: str) -> str:
    """Id da operação de estorno no BC Limites, DERIVADO da original (UUIDv5).

    Estornos concorrentes da mesma autorização viram UMA operação idempotente no limite.
    """
    return str(uuid.uuid5(NAMESPACE_ESTORNOS, id_autorizacao_original))


def id_operacao_de_expiracao(id_reserva: str) -> str:
    """Id da liberação por expiração: o expirador que cair no meio repete a MESMA operação."""
    return str(uuid.uuid5(NAMESPACE_EXPIRACOES, id_reserva))


@dataclass(frozen=True, slots=True)
class EscopoDeIdempotencia:
    client_id: str
    metodo: str
    uri: str
    chave: str

    def id_autorizacao(self) -> str:
        nome = "|".join((self.client_id, self.metodo.upper(), self.uri, self.chave.lower()))
        return str(uuid.uuid5(NAMESPACE_AUTORIZACOES, nome))


def impressao_digital(pedido: PedidoDeAutorizacao) -> str:
    canonico = json.dumps(
        pedido.forma_canonica(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonico.encode()).hexdigest()


class Reexecucao(StrEnum):
    REPLAY = "REPLAY"  # devolve o resultado original
    CHAVE_REUTILIZADA = "CHAVE_REUTILIZADA"  # 422: mesma chave, pedido diferente
    EM_ANDAMENTO = "EM_ANDAMENTO"  # 409: outra execução detém o lease
    RETOMAR = "RETOMAR"  # lease expirou sem conclusão: esta requisição assume e continua
    ENCERRADA_SEM_RESULTADO = "ENCERRADA_SEM_RESULTADO"  # 409: compensada pelo reconciliador
    EM_RECONCILIACAO = "EM_RECONCILIACAO"  # 409: fora do prazo de reaplicação, decisão humana


def avaliar_reexecucao(existente: Autorizacao, impressao: str, agora: datetime) -> Reexecucao:
    """O que fazer quando a chave já existe. A ordem importa: pedido diferente vence tudo."""
    if existente.impressao_digital != impressao:
        return Reexecucao.CHAVE_REUTILIZADA
    if existente.resultado is not None:
        return Reexecucao.REPLAY
    if existente.status in (StatusAutorizacao.COMPENSADA, StatusAutorizacao.COMPENSANDO):
        return Reexecucao.ENCERRADA_SEM_RESULTADO  # o reconciliador já decidiu desfazer
    if existente.status is StatusAutorizacao.RECONCILIACAO_MANUAL:
        return Reexecucao.EM_RECONCILIACAO
    if existente.lease_ate > agora:
        return Reexecucao.EM_ANDAMENTO
    if not existente.pode_reaplicar(agora):
        return Reexecucao.EM_RECONCILIACAO  # retomar reenviaria ao limite fora da guarda
    return Reexecucao.RETOMAR
