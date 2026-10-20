"""Caso de uso ConsultarAutorizacao: leitura do estado atual (GET), sem efeitos colaterais."""

from __future__ import annotations

from dataclasses import dataclass

from autorizacao.application.portas import RepositorioDeAutorizacoes
from autorizacao.domain.autorizacao import Autorizacao


class AutorizacaoNaoEncontrada(Exception):
    """Inexistente, de outro contrato ou de OUTRO CANAL (não revela a existência) → 404."""


@dataclass(frozen=True, slots=True)
class ConsultarAutorizacao:
    repositorio: RepositorioDeAutorizacoes

    def executar(self, id_contrato: str, id_autorizacao: str, client_id: str) -> Autorizacao:
        """Só o canal que criou a autorização a enxerga (o client_id vem do token, injetado pelo gateway)."""
        autorizacao = self.repositorio.obter(id_autorizacao)
        if (
            autorizacao is None
            or autorizacao.id_contrato != id_contrato
            or autorizacao.client_id != client_id
        ):
            raise AutorizacaoNaoEncontrada(id_autorizacao)
        return autorizacao
