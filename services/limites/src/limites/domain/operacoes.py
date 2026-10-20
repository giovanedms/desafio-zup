"""Operações sobre o Limite e o Plano declarativo que cada uma produz.

Cada operação sabe dizer O QUE precisa ser verdade (pré-condições) e O QUE muda (efeitos) sem ler
o estado atual. Isso permite que o mesmo Plano seja:
  - aplicado em memória sobre o agregado (`Limite.aplicar`), e
  - compilado numa única escrita condicional atômica no DynamoDB (adapter de saída).
A regra de negócio fica num lugar só (aqui); o banco apenas a executa de forma atômica.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from limites.domain.dinheiro import ZERO, ValorMonetarioInvalido, dinheiro_positivo


class TipoDeOperacao(StrEnum):
    RESERVAR = "RESERVAR"
    DEBITAR = "DEBITAR"
    CREDITAR = "CREDITAR"
    LIBERAR_RESERVA = "LIBERAR_RESERVA"
    CAPTURAR_RESERVA = "CAPTURAR_RESERVA"
    ESTORNAR_DEBITO = "ESTORNAR_DEBITO"


@dataclass(frozen=True, slots=True)
class Precondicoes:
    """O que precisa ser verdade no instante da escrita (avaliado atomicamente)."""

    contrato_ativo: bool = False
    disponivel_minimo: Decimal | None = None
    utilizado_minimo: Decimal | None = None
    reserva_existente: tuple[str, Decimal] | None = None  # (id_reserva, valor exato)
    reserva_inexistente: str | None = None  # id_reserva que ainda não pode existir


@dataclass(frozen=True, slots=True)
class Efeitos:
    """O que muda. `disponivel` não aparece: é sempre concedido − utilizado − reservado."""

    delta_utilizado: Decimal = ZERO
    delta_reservado: Decimal = ZERO
    criar_reserva: tuple[str, Decimal] | None = None
    remover_reserva: str | None = None


@dataclass(frozen=True, slots=True)
class Plano:
    id_operacao: str
    precondicoes: Precondicoes
    efeitos: Efeitos


class OperacaoDeLimite(ABC):
    """Comando sobre o limite. Nova operação = nova subclasse (OCP); adapters não mudam."""

    id_operacao: str

    @property
    @abstractmethod
    def tipo(self) -> TipoDeOperacao: ...

    @property
    @abstractmethod
    def valor(self) -> Decimal: ...

    @abstractmethod
    def planejar(self) -> Plano: ...


@dataclass(frozen=True, slots=True)
class Reservar(OperacaoDeLimite):
    """Hold: compromete limite sem consumir. Exige contrato ATIVO e disponível suficiente."""

    id_operacao: str
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.RESERVAR

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(
                contrato_ativo=True,
                disponivel_minimo=self.quantia,
                # Idempotência enquanto o hold existir (até 7 dias), não só na janela da guarda.
                reserva_inexistente=self.id_operacao,
            ),
            Efeitos(delta_reservado=self.quantia, criar_reserva=(self.id_operacao, self.quantia)),
        )


@dataclass(frozen=True, slots=True)
class Debitar(OperacaoDeLimite):
    """Consumo imediato do limite. Exige contrato ATIVO e disponível suficiente."""

    id_operacao: str
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.DEBITAR

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(contrato_ativo=True, disponivel_minimo=self.quantia),
            Efeitos(delta_utilizado=self.quantia),
        )


@dataclass(frozen=True, slots=True)
class Creditar(OperacaoDeLimite):
    """Recompõe limite utilizado (ex.: pagamento). Não pode deixar o utilizado negativo.

    Não exige contrato ATIVO: pagamento de contrato bloqueado continua recompondo o limite.
    """

    id_operacao: str
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.CREDITAR

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(utilizado_minimo=self.quantia),
            Efeitos(delta_utilizado=-self.quantia),
        )


@dataclass(frozen=True, slots=True)
class LiberarReserva(OperacaoDeLimite):
    """Desfaz um hold (estorno de reserva, expiração ou compensação de órfã).

    O valor é informado pelo chamador e conferido contra o hold gravado: liberar com valor
    diferente do reservado é recusado (RESERVA_DIVERGENTE). Hold inexistente é recusado
    (RESERVA_NAO_ENCONTRADA), não tratado como sucesso: quem remove o hold primeiro (captura,
    estorno ou expiração) vence, e os demais precisam saber que perderam.
    """

    id_operacao: str
    id_reserva: str
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.LIBERAR_RESERVA

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(reserva_existente=(self.id_reserva, self.quantia)),
            Efeitos(delta_reservado=-self.quantia, remover_reserva=self.id_reserva),
        )


@dataclass(frozen=True, slots=True)
class CapturarReserva(OperacaoDeLimite):
    """Converte um hold em consumo, numa escrita só: o hold some, o capturado vira utilizado.

    Captura parcial é permitida (posto de combustível, hotel): o que sobra do hold volta ao
    disponível na mesma escrita. Não exige contrato ATIVO: a obrigação foi aprovada quando o
    hold foi feito, e bloquear o contrato depois não cancela o que o estabelecimento já entregou.
    """

    id_operacao: str
    id_reserva: str
    valor_reservado: Decimal
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "valor_reservado", dinheiro_positivo(self.valor_reservado))
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))
        if self.quantia > self.valor_reservado:
            raise ValorMonetarioInvalido("captura maior que o valor reservado")

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.CAPTURAR_RESERVA

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(reserva_existente=(self.id_reserva, self.valor_reservado)),
            Efeitos(
                delta_reservado=-self.valor_reservado,
                delta_utilizado=self.quantia,
                remover_reserva=self.id_reserva,
            ),
        )


@dataclass(frozen=True, slots=True)
class EstornarDebito(OperacaoDeLimite):
    """Reverte um débito anterior: devolve o valor ao disponível."""

    id_operacao: str
    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", dinheiro_positivo(self.quantia))

    @property
    def tipo(self) -> TipoDeOperacao:
        return TipoDeOperacao.ESTORNAR_DEBITO

    @property
    def valor(self) -> Decimal:
        return self.quantia

    def planejar(self) -> Plano:
        return Plano(
            self.id_operacao,
            Precondicoes(utilizado_minimo=self.quantia),
            Efeitos(delta_utilizado=-self.quantia),
        )
