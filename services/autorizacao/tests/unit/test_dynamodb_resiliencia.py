"""Adapter DynamoDB da autorização sob estrangulamento, timeout e transação cancelada (sem rede: botocore Stubber).

Falha transitória vira `ArmazenamentoIndisponivel` (→ 503 com Retry-After, como no `limites`); o cancelamento de
TransactWriteItems é classificado por (índice do item, código), não pela posição de um único motivo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest
from autorizacao.adapters.inbound.http import criar_app
from autorizacao.adapters.outbound.dynamodb import (
    RepositorioDeAutorizacoesDynamoDB,
    _item_do_outbox,
)
from autorizacao.application.autorizar import AutorizarTransacao
from autorizacao.application.consultar import ConsultarAutorizacao
from autorizacao.application.portas import (
    ArmazenamentoIndisponivel,
    DesfechoNoLimite,
    LeasePerdido,
    OperacaoNoLimite,
    OriginalJaEncerrada,
    ResultadoNoLimite,
)
from autorizacao.domain.autorizacao import VALIDADE_DA_RESERVA, Autorizacao
from autorizacao.domain.eventos import SaldosDoLimite
from autorizacao.domain.pedido import PedidoDeAutorizacao, TipoOperacao
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber
from fastapi.testclient import TestClient

AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
SALDOS = SaldosDoLimite(
    Decimal("1000.00"), Decimal("0.00"), Decimal("150.00"), Decimal("850.00"), 2
)
TRANSITORIOS = [
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "InternalServerError",
    "ServiceUnavailable",
    "TransactionConflictException",
]


def _cliente() -> tuple[Any, Stubber]:
    # Credenciais fictícias explícitas: o Stubber responde antes de qualquer chamada de rede.
    cliente = boto3.client(
        "dynamodb",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 1}),
    )
    return cliente, Stubber(cliente)


def _repositorio(cliente: Any) -> RepositorioDeAutorizacoesDynamoDB:
    return RepositorioDeAutorizacoesDynamoDB(cliente, "autorizacoes", "autorizacoes-outbox")


def _nova(tipo: TipoOperacao = TipoOperacao.RESERVA, id_autorizacao: str = "aut-1") -> Autorizacao:
    return Autorizacao.iniciar(
        PedidoDeAutorizacao("CTR-1", "0001-1", Decimal("150.00"), "BRL", tipo),
        id_autorizacao=id_autorizacao,
        impressao_digital="imp",
        client_id="canal-pos",
        agora=AGORA,
        correlation_id=str(uuid.uuid4()),
        traceparent=None,
    )


def _cancelada(stub: Stubber, *motivos: str) -> None:
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        modeled_fields={"CancellationReasons": [{"Code": m} for m in motivos]},
    )


@pytest.mark.parametrize("codigo", TRANSITORIOS)
def test_estrangulamento_ou_conflito_vira_armazenamento_indisponivel(codigo: str) -> None:
    cliente, stub = _cliente()
    stub.add_client_error("put_item", service_error_code=codigo, http_status_code=400)

    with stub, pytest.raises(ArmazenamentoIndisponivel):
        _repositorio(cliente).registrar(_nova())


def test_erro_que_nao_e_transitorio_continua_sendo_erro() -> None:
    cliente, stub = _cliente()
    stub.add_client_error("get_item", service_error_code="ValidationException")

    with stub, pytest.raises(ClientError):
        _repositorio(cliente).obter("aut-1")


def test_timeout_no_commit_incerto_vira_armazenamento_indisponivel() -> None:
    cliente, _ = _cliente()

    def timeout(**_: object) -> None:
        raise ReadTimeoutError(endpoint_url="https://dynamodb.sa-east-1.amazonaws.com")

    cliente.transact_write_items = timeout
    aprovada = _nova().aprovar(SALDOS, AGORA, str(uuid.uuid4()))

    with pytest.raises(ArmazenamentoIndisponivel):
        _repositorio(cliente).concluir(aprovada, None)


def _estorno_e_original() -> tuple[Autorizacao, Autorizacao]:
    original = _nova(TipoOperacao.DEBITO, "aut-orig").aprovar(SALDOS, AGORA, str(uuid.uuid4()))
    estorno = Autorizacao.iniciar(
        PedidoDeAutorizacao(
            "CTR-1",
            "0001-1",
            Decimal("150.00"),
            "BRL",
            TipoOperacao.ESTORNO,
            id_autorizacao_original="aut-orig",
        ),
        id_autorizacao="aut-est",
        impressao_digital="imp",
        client_id="canal-pos",
        agora=AGORA,
        correlation_id=str(uuid.uuid4()),
        traceparent=None,
    )
    aprovado = estorno.aprovar(SALDOS, AGORA, str(uuid.uuid4()), original)
    return aprovado, original.estornar(AGORA)


@pytest.mark.parametrize(
    ("motivos", "esperada"),
    [
        (("ConditionalCheckFailed", "None", "None"), LeasePerdido),
        (("None", "ConditionalCheckFailed", "None"), OriginalJaEncerrada),
        (
            ("ConditionalCheckFailed", "TransactionConflict", "None"),
            LeasePerdido,
        ),  # condição decide
        (("None", "TransactionConflict", "None"), ArmazenamentoIndisponivel),
        (("ThrottlingError", "None", "None"), ArmazenamentoIndisponivel),
        (("None", "None", "ProvisionedThroughputExceeded"), ArmazenamentoIndisponivel),
    ],
)
def test_concluir_classifica_o_cancelamento_por_indice_e_codigo(
    motivos: tuple[str, ...], esperada: type[Exception]
) -> None:
    cliente, stub = _cliente()
    _cancelada(stub, *motivos)
    estorno, original = _estorno_e_original()

    with stub, pytest.raises(esperada):
        _repositorio(cliente).concluir(estorno, original)


def test_condicao_no_outbox_e_defeito_e_sobe_como_esta() -> None:
    """Id de evento repetido no outbox não é estado a tratar (é bug): nem LeasePerdido nem 503."""
    cliente, stub = _cliente()
    _cancelada(stub, "None", "ConditionalCheckFailed")
    aprovada = _nova().aprovar(SALDOS, AGORA, str(uuid.uuid4()))

    with stub, pytest.raises(ClientError, match="TransactionCanceled"):
        _repositorio(cliente).concluir(aprovada, None)


def _reserva_vencida() -> Autorizacao:
    return _nova().aprovar(SALDOS, AGORA, str(uuid.uuid4())).sem_eventos()


@pytest.mark.parametrize(
    ("motivos", "esperada"),
    [
        (("ConditionalCheckFailed", "None"), OriginalJaEncerrada),
        (("None", "TransactionConflict"), ArmazenamentoIndisponivel),
        (("ThrottlingError", "None"), ArmazenamentoIndisponivel),
    ],
)
def test_expirar_e_encaminhar_classificam_por_indice_e_codigo(
    motivos: tuple[str, ...], esperada: type[Exception]
) -> None:
    depois = AGORA + VALIDADE_DA_RESERVA
    expirada = _reserva_vencida().expirar(SALDOS, depois, str(uuid.uuid4()))
    encaminhada = _reserva_vencida().encaminhar_reserva_sem_hold(depois, str(uuid.uuid4()))
    cliente, stub = _cliente()
    _cancelada(stub, *motivos)
    _cancelada(stub, *motivos)

    with stub:
        with pytest.raises(esperada):
            _repositorio(cliente).registrar_expiracao(expirada)
        with pytest.raises(esperada):
            _repositorio(cliente).encaminhar_reserva(encaminhada)


def test_item_do_outbox_nao_tem_ttl() -> None:
    """Um evento não publicado sai do outbox publicado, nunca por idade (o TTL apagava sem rastro)."""
    (evento,) = _nova().aprovar(SALDOS, AGORA, str(uuid.uuid4())).eventos

    assert "ttl" not in _item_do_outbox(evento)


class LimiteQueAplica:
    def aplicar(self, id_contrato: str, operacao: OperacaoNoLimite) -> ResultadoNoLimite:
        return ResultadoNoLimite(DesfechoNoLimite.APLICADA, SALDOS)


class Relogio:
    def agora(self) -> datetime:
        return AGORA


class Ids:
    def novo_id(self) -> str:
        return str(uuid.uuid4())


@pytest.mark.parametrize(
    "falha",
    [("TransactionConflict",), ("ThrottlingError",)],
    ids=["conflito", "estrangulamento"],
)
def test_transacao_cancelada_por_conflito_no_post_e_503_com_retry_after(
    falha: tuple[str, ...],
) -> None:
    """O PoC H3 do painel: o concluir cancelado por conflito respondia 500 sem Retry-After."""
    cliente, stub = _cliente()
    stub.add_response("put_item", {})  # registrar EM_PROCESSAMENTO
    _cancelada(stub, *falha, "None")  # concluir
    repositorio = _repositorio(cliente)
    caso = AutorizarTransacao(repositorio, LimiteQueAplica(), Relogio(), Ids())  # type: ignore[arg-type]
    app = TestClient(criar_app(caso, ConsultarAutorizacao(repositorio), 100))

    with stub:
        resposta = app.post(
            "/v1/contratos/CTR-1/autorizacoes",
            content=b'{"id_conta": "0001-1", "valor": 150.00, "moeda": "BRL", "tipo_operacao": "RESERVA"}',
            headers={
                "Content-Type": "application/json",
                "X-Client-Id": "canal-pos",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    assert resposta.status_code == 503
    assert resposta.headers["retry-after"] == "1"
    assert resposta.json()["codigo"] == "SERVICO_INDISPONIVEL"
    assert "mesma Idempotency-Key" in resposta.json()["detail"]
