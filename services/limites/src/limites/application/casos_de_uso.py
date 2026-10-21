"""Casos de uso do BC Limites. Orquestram domínio e portas; não conhecem HTTP nem AWS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from limites.application.portas import (
    AplicacaoDeOperacoes,
    ConsultaDeLimites,
    ContratoNaoEncontrado,
    PrazoDoChamadorVencido,
    Relogio,
    SincronizacaoComLegado,
)
from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao
from limites.domain.limite import Limite
from limites.domain.operacoes import OperacaoDeLimite
from limites.domain.resultados import ResultadoDaOperacao


@dataclass(frozen=True, slots=True)
class AplicarOperacaoNoLimite:
    """Aplica uma operação no limite de um contrato (reservar, debitar, creditar, liberar...)."""

    repositorio: AplicacaoDeOperacoes
    relogio: Relogio

    def executar(
        self, id_contrato: str, operacao: OperacaoDeLimite, prazo: datetime | None = None
    ) -> ResultadoDaOperacao:
        """`prazo`: instante depois do qual o chamador já desistiu. Conferido UMA vez, aqui, já na thread que vai
        escrever (depois da espera na fila): requisição que chega vencida não escreve nada.

        O que isso limita é a espera na fila, não a escrita. A escrita que começa dentro do prazo pode cair depois
        dele: o adapter repete a escrita (retries do SDK, voltas de diagnóstico) e quem limita esse tempo são os
        timeouts e tentativas dele. Não se confere de novo entre as tentativas: elas acontecem dentro do adapter (as
        do SDK, dentro de uma única chamada), e depois de um timeout a tentativa anterior pode ter sido aplicada:
        desistir ali não impede a escrita de cair, e o 503 deixaria de poder dizer "nada foi aplicado"."""
        agora = self.relogio.agora()
        if prazo is not None and agora > prazo:
            raise PrazoDoChamadorVencido(id_contrato)
        return self.repositorio.aplicar(id_contrato, operacao, agora)


@dataclass(frozen=True, slots=True)
class ConsultarLimite:
    """Retrato atual do limite (leitura fortemente consistente)."""

    repositorio: ConsultaDeLimites

    def executar(self, id_contrato: str) -> Limite:
        limite = self.repositorio.obter(id_contrato)
        if limite is None:
            raise ContratoNaoEncontrado(id_contrato)
        return limite


@dataclass(frozen=True, slots=True)
class SincronizarComLegado:
    """Aplica uma atualização vinda do core legado (já traduzida pela ACL)."""

    repositorio: SincronizacaoComLegado

    def executar(self, atualizacao: AtualizacaoDoLegado) -> ResultadoDaSincronizacao:
        return self.repositorio.sincronizar(atualizacao)
