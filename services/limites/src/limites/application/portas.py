"""Portas (interfaces) que a aplicação exige do mundo externo — inversão de dependência (DIP).

Cada porta é pequena e específica (ISP): quem só consulta não depende de quem altera.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from limites.domain.legado import AtualizacaoDoLegado, ResultadoDaSincronizacao
from limites.domain.limite import Limite
from limites.domain.operacoes import OperacaoDeLimite
from limites.domain.resultados import ResultadoDaOperacao


class ContratoNaoEncontrado(LookupError):
    """Contrato ausente na réplica do core (fail-closed: nada é autorizado)."""


class ContencaoPersistente(RuntimeError):
    """O estado mudou várias vezes entre a falha da condição e o diagnóstico → 503 (retry seguro)."""


class ArmazenamentoIndisponivel(RuntimeError):
    """O banco estrangulou ou não respondeu depois dos retries do SDK → 503 (retry seguro: a guarda de
    idempotência absorve a escrita que talvez tenha sido aplicada)."""


class PrazoDoChamadorVencido(RuntimeError):
    """A requisição esperou na fila além do prazo que o chamador mandou: não aplica (o chamador já desistiu
    e pode estar decidindo sem esta escrita) → 503. Limita quanto tempo depois do envio uma escrita pode COMEÇAR;
    depois de começar, quem limita são os timeouts e tentativas do adapter (ver `AplicarOperacaoNoLimite.executar`)."""


class ContratoJaExiste(Exception):
    """Tentativa de criar de novo um limite já replicado."""


class Relogio(Protocol):
    def agora(self) -> datetime: ...


class ConsultaDeLimites(Protocol):
    def obter(self, id_contrato: str) -> Limite | None: ...


class AplicacaoDeOperacoes(Protocol):
    def aplicar(
        self, id_contrato: str, operacao: OperacaoDeLimite, agora: datetime
    ) -> ResultadoDaOperacao:
        """Aplica a operação de forma atômica e idempotente por `operacao.id_operacao`.

        Contrato (verificado pela mesma suíte nos dois adapters — LSP):
          - pré-condição atendida → APLICADA, com os saldos exatos pós-operação;
          - operação já aplicada → JA_APLICADA, sem reaplicar;
          - pré-condição não atendida → RECUSADA com motivo (sem efeito no estado, nem na guarda: o mesmo id
            corrigido, ex.: com a conta certa, ainda aplica);
          - contrato inexistente → ContratoNaoEncontrado;
          - concorrência no mesmo contrato nunca produz erro: escritas são serializadas.
        """
        ...


class CadastroDeLimites(Protocol):
    def criar(self, limite: Limite) -> None:
        """Cria o limite replicado do core. `ContratoJaExiste` se já houver."""
        ...


class SincronizacaoComLegado(Protocol):
    def sincronizar(self, atualizacao: AtualizacaoDoLegado) -> ResultadoDaSincronizacao:
        """Aplica a atualização do legado com compare-and-set nos campos do legado (concedido, status, id_conta,
        tipo_produto, versao_legado). Não conflita com o caminho quente: reservas mexem em
        utilizado/reservado/disponível com expressões comutativas."""
        ...


class RepositorioDeLimites(ConsultaDeLimites, AplicacaoDeOperacoes, CadastroDeLimites, Protocol):
    """Agregação conveniente para os adapters que implementam tudo."""
