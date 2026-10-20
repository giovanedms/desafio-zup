"""Agregado Limite: saldo de crédito de um contrato, com reservas ativas e guarda de idempotência.

Responsabilidade única (SRP): este módulo muda quando muda a regra do saldo (invariantes, reservas,
janela de deduplicação), não quando muda o banco, o protocolo HTTP ou o layout do legado.

Invariantes (verificadas por `verificar_invariantes` e pelos testes property-based):
  I1  disponivel = concedido − utilizado − reservado
  I2  utilizado ≥ 0 e reservado ≥ 0
  I3  reservado = Σ valor das reservas ativas
  I4  operação que exige disponível só é aplicada se, depois dela, disponivel ≥ 0
  I5  versao cresce exatamente 1 por operação aplicada
  I6  cada id_operacao é aplicado no máximo uma vez (dentro da janela de deduplicação)
`disponivel` só fica negativo se o core reduzir o limite concedido abaixo do comprometido
(decisão de crédito do legado prevalece); nesse caso nenhuma nova reserva/débito passa (I4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from limites.domain.dinheiro import ZERO, dinheiro
from limites.domain.operacoes import OperacaoDeLimite, Plano
from limites.domain.resultados import Desfecho, MotivoRecusa, Saldos

# Janela da guarda de idempotência de operações (DEBITAR/CREDITAR/ESTORNAR/LIBERAR). Precisa cobrir o
# maior intervalo entre duas tentativas com o mesmo id: lease 10 s + graça do reconciliador 60 s + ciclo
# 30 s ≈ 100 s. 5 min dá margem de 3x. O item guarda ~taxa_do_contrato × 300 s entradas (ADR-0005/0016).
JANELA_DE_DEDUPLICACAO = timedelta(minutes=5)


class StatusContrato(StrEnum):
    ATIVO = "ATIVO"
    BLOQUEADO = "BLOQUEADO"
    ENCERRADO = "ENCERRADO"


class TipoProduto(StrEnum):
    CREDITO = "CREDITO"
    INVESTIMENTO = "INVESTIMENTO"
    SEGURO = "SEGURO"
    CONSORCIO = "CONSORCIO"


class InvarianteViolada(AssertionError):
    """Estado impossível: indica bug, nunca regra de negócio."""


class PrecondicaoNaoAtendida(Exception):
    """O plano não cabe no estado atual (`Limite.diagnosticar` explica por quê)."""


@dataclass(frozen=True, slots=True)
class Limite:
    id_contrato: str
    id_conta: str
    tipo_produto: TipoProduto
    status: StatusContrato
    concedido: Decimal
    utilizado: Decimal = ZERO
    reservado: Decimal = ZERO
    disponivel: Decimal = ZERO
    versao: int = 0
    versao_legado: int = 0
    reservas: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    operacoes_recentes: Mapping[str, datetime] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def novo(
        cls,
        id_contrato: str,
        id_conta: str,
        tipo_produto: TipoProduto,
        concedido: Decimal,
        *,
        status: StatusContrato = StatusContrato.ATIVO,
        versao_legado: int = 0,
    ) -> Limite:
        """Limite recém-replicado do core: nada utilizado nem reservado."""
        valor = dinheiro(concedido)
        return cls(
            id_contrato=id_contrato,
            id_conta=id_conta,
            tipo_produto=tipo_produto,
            status=status,
            concedido=valor,
            disponivel=valor,
            versao=1,
            versao_legado=versao_legado,
        )

    @property
    def saldos(self) -> Saldos:
        return Saldos(
            limite_concedido=self.concedido,
            limite_utilizado=self.utilizado,
            saldo_reservado=self.reservado,
            limite_disponivel=self.disponivel,
            versao=self.versao,
        )

    def ja_aplicou(self, id_operacao: str) -> bool:
        return id_operacao in self.operacoes_recentes

    def tem_reserva(self, id_reserva: str) -> bool:
        return id_reserva in self.reservas

    def atende(self, plano: Plano) -> bool:
        """Avalia as pré-condições do plano (a mesma lógica vira a condição no DynamoDB)."""
        p = plano.precondicoes
        if self.ja_aplicou(plano.id_operacao):
            return False
        if p.contrato_ativo and self.status is not StatusContrato.ATIVO:
            return False
        if p.disponivel_minimo is not None and self.disponivel < p.disponivel_minimo:
            return False
        if p.utilizado_minimo is not None and self.utilizado < p.utilizado_minimo:
            return False
        if p.reserva_existente is not None:
            id_reserva, valor = p.reserva_existente
            if self.reservas.get(id_reserva) != valor:
                return False
        return p.reserva_inexistente is None or p.reserva_inexistente not in self.reservas

    def aplicar(self, plano: Plano, agora: datetime) -> Limite:
        """Aplica o plano e devolve o novo estado; `PrecondicaoNaoAtendida` se não couber."""
        if not self.atende(plano):
            raise PrecondicaoNaoAtendida(plano.id_operacao)
        e = plano.efeitos
        reservas = dict(self.reservas)
        if e.criar_reserva is not None:
            id_reserva, valor = e.criar_reserva
            reservas[id_reserva] = valor
        if e.remover_reserva is not None:
            reservas.pop(e.remover_reserva)
        utilizado = self.utilizado + e.delta_utilizado
        reservado = self.reservado + e.delta_reservado
        operacoes = dict(self.operacoes_recentes)
        operacoes[plano.id_operacao] = agora
        novo = replace(
            self,
            utilizado=utilizado,
            reservado=reservado,
            disponivel=self.concedido - utilizado - reservado,
            versao=self.versao + 1,
            reservas=MappingProxyType(reservas),
            operacoes_recentes=MappingProxyType(operacoes),
        )
        novo.verificar_invariantes()
        return novo

    def diagnosticar(self, operacao: OperacaoDeLimite) -> tuple[Desfecho, MotivoRecusa | None]:
        """Explica por que a operação não foi aplicada neste estado.

        Usado pelos DOIS adapters (memória e DynamoDB, que recebe o item antigo via
        ReturnValuesOnConditionCheckFailure) — a classificação nunca é duplicada.
        A ordem importa: idempotência vem antes de qualquer recusa.
        """
        plano = operacao.planejar()
        p = plano.precondicoes
        if self.ja_aplicou(plano.id_operacao):
            return Desfecho.JA_APLICADA, None
        if p.reserva_inexistente is not None and p.reserva_inexistente in self.reservas:
            return Desfecho.JA_APLICADA, None  # RESERVAR repetido com o hold ainda vivo
        if p.reserva_existente is not None:
            id_reserva, valor = p.reserva_existente
            if id_reserva not in self.reservas:
                # O hold é o recurso disputado por captura, estorno e expiração: quem o remove vence.
                # Esta operação não foi aplicada (a guarda acima teria dito JA_APLICADA), então perdeu.
                return Desfecho.RECUSADA, MotivoRecusa.RESERVA_NAO_ENCONTRADA
            if self.reservas[id_reserva] != valor:
                return Desfecho.RECUSADA, MotivoRecusa.RESERVA_DIVERGENTE
        if p.contrato_ativo and self.status is not StatusContrato.ATIVO:
            return Desfecho.RECUSADA, MotivoRecusa.CONTRATO_INATIVO
        if p.disponivel_minimo is not None and self.disponivel < p.disponivel_minimo:
            return Desfecho.RECUSADA, MotivoRecusa.LIMITE_INSUFICIENTE
        if p.utilizado_minimo is not None and self.utilizado < p.utilizado_minimo:
            return Desfecho.RECUSADA, MotivoRecusa.CREDITO_EXCEDE_UTILIZADO
        if self.atende(plano):
            raise InvarianteViolada("diagnosticar chamado para operação aplicável")
        raise InvarianteViolada(f"pré-condição sem diagnóstico: {plano.precondicoes}")

    def operacoes_expiradas(self, agora: datetime) -> tuple[str, ...]:
        """Ids da guarda que já saíram da janela (o adapter os remove de carona na próxima escrita)."""
        return tuple(
            op
            for op, quando in self.operacoes_recentes.items()
            if agora - quando > JANELA_DE_DEDUPLICACAO
        )

    def compactar(self, agora: datetime) -> Limite:
        """Tira da guarda de idempotência as operações mais velhas que a janela de deduplicação."""
        recentes = {
            op: quando
            for op, quando in self.operacoes_recentes.items()
            if agora - quando <= JANELA_DE_DEDUPLICACAO
        }
        return replace(self, operacoes_recentes=MappingProxyType(recentes))

    def verificar_invariantes(self) -> None:
        if self.disponivel != self.concedido - self.utilizado - self.reservado:
            raise InvarianteViolada("I1: disponivel ≠ concedido − utilizado − reservado")
        if self.utilizado < ZERO or self.reservado < ZERO:
            raise InvarianteViolada("I2: utilizado ou reservado negativo")
        if self.reservado != sum(self.reservas.values(), ZERO):
            raise InvarianteViolada("I3: reservado ≠ soma das reservas ativas")
