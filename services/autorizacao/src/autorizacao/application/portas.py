"""Portas do BC Autorização (DIP + ISP) e os erros que atravessam a camada de aplicação."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from autorizacao.domain.autorizacao import Autorizacao
from autorizacao.domain.eventos import SaldosDoLimite

# --- Erros de aplicação (o adapter HTTP traduz para problem+json) ------------------------------


class ChaveReutilizada(Exception):
    """Mesma Idempotency-Key com pedido diferente → 422."""


class EmAndamento(Exception):
    """Mesma chave ainda em processamento → 409 com Retry-After."""

    def __init__(self, segundos_para_tentar: int) -> None:
        super().__init__("requisição em andamento")
        self.segundos_para_tentar = max(1, segundos_para_tentar)


class EmReconciliacao(Exception):
    """Fora do prazo de reaplicação ou em RECONCILIACAO_MANUAL: o resultado sai de análise humana → 409
    `conflito-de-estado` SEM Retry-After (repetir não muda nada; o canal consulta com GET depois)."""


class EncerradaSemResultado(Exception):
    """A chave foi compensada pelo reconciliador (cliente nunca confirmou) → 409; use nova chave."""


class AutorizacaoOriginalInvalida(Exception):
    """Estorno de autorização inexistente, de outro contrato, não estornável ou com valor diferente → 422."""


class ConflitoDeEstado(Exception):
    """Estorno ou captura de autorização que já saiu de APROVADA (estornada/capturada/expirada) → 409."""


class LimitesIndisponivel(Exception):
    """Timeout, circuit breaker aberto ou erro 5xx no BC Limites → 503 (retomável pela mesma chave)."""


class ArmazenamentoIndisponivel(Exception):
    """O DynamoDB da autorização estrangulou, não respondeu ou cancelou a transação por conflito, depois dos
    retries do SDK → 503 com Retry-After. Repetir com a mesma Idempotency-Key é seguro: o id é determinístico,
    toda escrita é condicional e o commit incerto do TransactWriteItems é idempotente pelo ClientRequestToken."""


class LeasePerdido(Exception):
    """Outra execução assumiu (ou o reconciliador compensou) antes desta concluir."""


class OriginalJaEncerrada(Exception):
    """Na conclusão do estorno/captura/expiração, a original já não estava APROVADA (outro venceu)."""


# --- Tradução para o BC Limites (Customer/Supplier) ------------------------------------------


class TipoNoLimite(StrEnum):
    RESERVAR = "RESERVAR"
    DEBITAR = "DEBITAR"
    CREDITAR = "CREDITAR"
    LIBERAR_RESERVA = "LIBERAR_RESERVA"
    CAPTURAR_RESERVA = "CAPTURAR_RESERVA"
    ESTORNAR_DEBITO = "ESTORNAR_DEBITO"


@dataclass(frozen=True, slots=True)
class OperacaoNoLimite:
    tipo: TipoNoLimite
    id_operacao: str
    valor: Decimal
    id_reserva: str | None = None
    valor_reservado: Decimal | None = None  # só na captura: o limite confere contra o hold gravado
    # Conta declarada pelo canal: o limite confere contra a titular do contrato (≠ → CONTA_DIVERGENTE). Só nas
    # operações pedidas pelo canal; as automáticas (expiração, compensação) não levam: com a portabilidade a conta
    # da réplica muda, e uma liberação com a conta antiga seria recusada e prenderia o hold.
    id_conta: str | None = None


class DesfechoNoLimite(StrEnum):
    APLICADA = "APLICADA"
    JA_APLICADA = "JA_APLICADA"
    RECUSADA = "RECUSADA"


@dataclass(frozen=True, slots=True)
class ResultadoNoLimite:
    desfecho: DesfechoNoLimite
    saldos: SaldosDoLimite
    motivo: str | None = None


class ContratoInexistente(Exception):
    """O BC Limites não conhece o contrato (404 lá) → recusa registrada CONTRATO_NAO_ENCONTRADO."""


@dataclass(frozen=True, slots=True)
class EstadoNoLimite:
    """Resposta da consulta só de leitura: o id está na guarda (aplicado há menos que a janela)? O hold com
    esse id está ativo? É o que o reconciliador pergunta antes de desfazer, em vez de aplicar para saber."""

    na_guarda: bool
    reserva_ativa: bool


class GatewayDeLimites(Protocol):
    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        """Raises ContratoInexistente ou LimitesIndisponivel."""
        ...

    def consultar_operacao(self, id_contrato: str, id_operacao: str) -> EstadoNoLimite:
        """Leitura consistente, nada é escrito. Raises ContratoInexistente ou LimitesIndisponivel."""
        ...


# --- Persistência --------------------------------------------------------------------------------


class LeituraDeAutorizacoes(Protocol):
    """Só leitura (ISP): quem consulta não depende de quem grava, como `ConsultaDeLimites` no BC Limites."""

    def obter(self, id_autorizacao: str) -> Autorizacao | None: ...


class RepositorioDeAutorizacoes(LeituraDeAutorizacoes, Protocol):
    def registrar(self, autorizacao: Autorizacao) -> Autorizacao | None:
        """Grava se o id ainda não existe (condicional). Devolve None se gravou; a existente se não."""
        ...

    def assumir(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        """Grava o novo lease SE o gravado ainda é `lease_anterior` (compare-and-set). False se perdeu."""
        ...

    def concluir(self, autorizacao: Autorizacao, original_encerrada: Autorizacao | None) -> None:
        """Grava o estado final + eventos de domínio no outbox, numa escrita atômica.

        Condições: a gravada está EM_PROCESSAMENTO com o MESMO lease (dono do lease) → senão LeasePerdido.
        Com `original_encerrada` (ESTORNADA ou CAPTURADA): grava também o novo status da original,
        condicionado a ela ainda estar APROVADA → senão OriginalJaEncerrada.
        """
        ...

    def marcar_estorno(
        self, id_original: str, id_estorno: str, substituir: str | None = None
    ) -> str | None:
        """CAS na original APROVADA: grava `estorno_em_curso = id_estorno` se a marca estiver livre, já
        for deste estorno, ou for `substituir` (marca velha de um estorno que terminou sem aprovação).

        Devolve None se marcou; senão o id do estorno que segura a marca, ou "" se a original já não
        está APROVADA. Um estorno por original em voo de cada vez: o id da operação de estorno é
        derivado da original, e dois estornos separados por mais que a guarda do limite reaplicariam.
        """
        ...


class RegistroDeCompensacao(Protocol):
    def regravar(self, autorizacao: Autorizacao, lease_anterior: datetime) -> bool:
        """Grava o novo estado de uma autorização EM CURSO (EM_PROCESSAMENTO/COMPENSANDO) SE o lease gravado
        ainda é `lease_anterior` (CAS). False se outra execução assumiu. Usado a cada passo da compensação."""
        ...


class BuscaDeOrfas(Protocol):
    def listar_orfas(self, lease_vencido_antes_de: datetime, limite: int) -> list[Autorizacao]:
        """Autorizações EM_PROCESSAMENTO cujo lease venceu antes do instante dado (mais antigas primeiro)."""
        ...


class ExpiracaoDeReservas(Protocol):
    def listar_reservas_vencidas(
        self, vencidas_antes_de: datetime, limite: int
    ) -> list[Autorizacao]:
        """RESERVAs ainda APROVADAS cuja validade terminou antes do instante dado (mais antigas primeiro)."""
        ...

    def encaminhar_reserva(self, encaminhada: Autorizacao) -> None:
        """Grava a reserva em RECONCILIACAO_MANUAL (sai do índice de vencimentos, entra na fila humana, sem
        TTL) + evento no outbox, numa escrita atômica condicionada a ela ainda estar APROVADA → senão
        OriginalJaEncerrada (o vencedor da disputa encerrou a reserva nesse meio-tempo)."""
        ...

    def registrar_expiracao(self, expirada: Autorizacao) -> None:
        """Grava a reserva EXPIRADA + eventos no outbox, numa escrita atômica, condicionada a ela ainda
        estar APROVADA → senão OriginalJaEncerrada (captura ou estorno chegou primeiro)."""
        ...


class Relogio(Protocol):
    def agora(self) -> datetime: ...


class GeradorDeIds(Protocol):
    def novo_id(self) -> str: ...
