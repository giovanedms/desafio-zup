"""Gateway HTTP para o BC Limites (contracts/openapi/limites-interno-v1.yaml).

Resiliência: disjuntor (fail-fast) + retry curto com backoff "full jitter" + timeouts curtos no
httpx.Client (configurados no composition root). O retry é SEGURO porque a operação é idempotente por
`id_operacao`: repetir devolve JA_APLICADA com os saldos atuais, nunca aplica duas vezes.

Prazo propagado: cada tentativa de aplicar leva `X-Prazo-Ms` (agora + PRAZO_POR_TENTATIVA). O limites não
começa a escrever se o prazo venceu enquanto a requisição esperava na fila dele, e a escrita que começou
termina dentro do orçamento de timeouts e retries do limites (ADR-0006): é isso que torna verdadeira a premissa
de tempo do reconciliador.

Dinheiro: corpo serializado com simplejson (Decimal como número exato) e resposta lida com
`parse_float=Decimal`; float nunca toca o valor.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import simplejson
from plataforma.observabilidade import correlation_id_atual, traceparent_atual

from autorizacao.adapters.outbound.disjuntor import Disjuntor
from autorizacao.application.portas import (
    ContratoInexistente,
    DesfechoNoLimite,
    EstadoNoLimite,
    LimitesIndisponivel,
    OperacaoNoLimite,
    ResultadoNoLimite,
)
from autorizacao.domain.eventos import SaldosDoLimite

# Orçamento de uma tentativa: timeout de conexão + leitura do httpx (composition root) com folga.
PRAZO_POR_TENTATIVA_S = 2.0


class GatewayDeLimitesHttp:
    def __init__(
        self,
        cliente: httpx.Client,
        disjuntor: Disjuntor,
        tentativas: int = 2,
        *,
        dormir: Callable[[float], None] = time.sleep,
        aleatorio: Callable[[], float] = random.random,
        relogio: Callable[[], float] = time.time,
    ) -> None:
        self._cliente = cliente
        self._disjuntor = disjuntor
        self._tentativas = tentativas
        self._dormir = dormir
        self._aleatorio = aleatorio
        self._relogio = relogio

    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        corpo: dict[str, Any] = {
            "id_operacao": operacao.id_operacao,
            "tipo": operacao.tipo.value,
            "valor": operacao.valor,
        }
        if operacao.id_reserva is not None:
            corpo["id_reserva"] = operacao.id_reserva
        if operacao.valor_reservado is not None:
            corpo["valor_reservado"] = operacao.valor_reservado
        if operacao.id_conta is not None:
            corpo["id_conta"] = (
                operacao.id_conta
            )  # conferida contra a titular do contrato no limites
        conteudo = simplejson.dumps(corpo, use_decimal=True).encode()
        resposta = self._chamar(
            lambda headers: self._cliente.post(
                f"/interno/v1/contratos/{id_contrato}/operacoes", content=conteudo, headers=headers
            ),
            com_prazo=True,
        )
        return self._interpretar(resposta)

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        resposta = self._chamar(
            lambda headers: self._cliente.get(
                f"/interno/v1/contratos/{id_contrato}/operacoes/{id_operacao}", headers=headers
            ),
            com_prazo=False,
        )
        self._disjuntor.registrar_sucesso()
        if resposta.status_code == 200:
            dados = resposta.json()
            return EstadoNoLimite(bool(dados["na_guarda"]), bool(dados["reserva_ativa"]))
        if resposta.status_code == 404:
            raise ContratoInexistente(resposta.url.path)
        raise RuntimeError(f"BC Limites respondeu {resposta.status_code} fora do contrato")

    def _chamar(
        self, enviar: Callable[[dict[str, str]], httpx.Response], *, com_prazo: bool
    ) -> httpx.Response:
        """Disjuntor + retry curto com full jitter. Devolve a 1ª resposta < 500; senão LimitesIndisponivel."""
        headers = {"Content-Type": "application/json"}
        if (correlacao := correlation_id_atual.get()) is not None:
            headers["X-Correlation-Id"] = correlacao
        if (traceparent := traceparent_atual.get()) is not None:
            headers["traceparent"] = traceparent
        for tentativa in range(self._tentativas):
            if not self._disjuntor.permitir():
                raise LimitesIndisponivel("circuit breaker aberto")
            if com_prazo:
                headers["X-Prazo-Ms"] = str(int((self._relogio() + PRAZO_POR_TENTATIVA_S) * 1000))
            try:
                resposta: httpx.Response | None = enviar(headers)
            except (httpx.TimeoutException, httpx.TransportError):
                resposta = None
            if resposta is not None and resposta.status_code < 500:
                return resposta
            self._disjuntor.registrar_falha()
            if tentativa + 1 < self._tentativas:
                self._dormir(self._aleatorio() * min(0.2, 0.05 * 2**tentativa))
        raise LimitesIndisponivel("BC Limites não respondeu após as tentativas")

    def _interpretar(self, resposta: httpx.Response) -> ResultadoNoLimite:
        # Respondeu (< 500) = está vivo. Registrar ANTES de interpretar: se a prova do meio-aberto
        # recebesse um 4xx inesperado (ou o parse falhasse) sem registrar nada, o disjuntor ficaria
        # preso em "prova em curso" e rejeitaria todas as chamadas para sempre.
        self._disjuntor.registrar_sucesso()
        if resposta.status_code == 200:
            dados = json.loads(resposta.text, parse_float=Decimal)
            s = dados["saldos"]
            return ResultadoNoLimite(
                DesfechoNoLimite(dados["resultado"]),
                SaldosDoLimite(
                    Decimal(s["limite_concedido"]),
                    Decimal(s["limite_utilizado"]),
                    Decimal(s["saldo_reservado"]),
                    Decimal(s["limite_disponivel"]),
                    int(s["versao"]),
                ),
                dados.get("motivo"),
            )
        if resposta.status_code == 404:
            raise ContratoInexistente(resposta.url.path)
        # 4xx inesperado = bug de contrato entre serviços; não é falha de disponibilidade.
        raise RuntimeError(f"BC Limites respondeu {resposta.status_code} fora do contrato")
