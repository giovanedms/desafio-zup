"""Handler da Lambda: resposta parcial de lote com o messageId certo, logs sem dado do evento, composição que falha
alto, prazo da reivindicação tirado do contexto, e o Terraform e o empacotamento apontando para este código."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from consumidores import lambda_sqs
from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.inbox_memoria import InboxEmMemoria
from consumidores.nucleo import ConsumirEvento, Situacao

RAIZ = next(p for p in Path(__file__).resolve().parents if (p / "contracts" / "events").is_dir())
AGORA_MS = 1_790_000_000_000


class Contexto:
    def __init__(self, restante_ms: int) -> None:
        self._restante_ms = restante_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._restante_ms


class InboxEspia(InboxEmMemoria):
    def __init__(self, falhar_em: str | None = None) -> None:
        super().__init__()
        self.prazos: list[int] = []
        self._falhar_em = falhar_em

    def reivindicar(self, chave: str, agora_ms: int, prazo_ms: int) -> Situacao:
        if self._falhar_em and chave.endswith(self._falhar_em):
            raise RuntimeError("ProvisionedThroughputExceededException")
        self.prazos.append(prazo_ms)
        return super().reivindicar(chave, agora_ms, prazo_ms)


def _registro(message_id: str, corpo: str) -> dict[str, Any]:
    return {"messageId": message_id, "body": corpo, "eventSource": "aws:sqs"}


@pytest.fixture
def instalar(
    monkeypatch: pytest.MonkeyPatch, catalogo: CatalogoDeEsquemas
) -> Callable[[InboxEmMemoria], None]:
    def _instalar(inbox: InboxEmMemoria) -> None:
        monkeypatch.setattr(
            lambda_sqs,
            "_consumir",
            ConsumirEvento("antifraude", catalogo, inbox, lambda_sqs._emitir, lambda: AGORA_MS),
        )

    return _instalar


def test_lote_misto_devolve_so_o_invalido_e_o_em_andamento(
    instalar: Callable[[InboxEmMemoria], None],
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    inbox = InboxEmMemoria()
    instalar(inbox)
    ok, ocupado = cloudevent(), cloudevent("transacao-recusada")
    inbox.reivindicar(f"{ocupado['source']}|{ocupado['id']}", AGORA_MS, AGORA_MS + 30_000)
    invalido = cloudevent(valor=-987.65)
    lote = {
        "Records": [
            _registro("m-ok", mensagem(ok)),
            _registro("m-invalido", mensagem(invalido)),
            _registro("m-duplicata", mensagem(ok)),
            _registro("m-ocupado", mensagem(ocupado)),
        ]
    }

    with caplog.at_level(logging.INFO, logger=lambda_sqs.log.name):
        resposta = lambda_sqs.handler(lote, Contexto(30_000))

    assert resposta == {
        "batchItemFailures": [{"itemIdentifier": "m-invalido"}, {"itemIdentifier": "m-ocupado"}]
    }
    linhas = [json.loads(r.getMessage()) for r in caplog.records]
    consumidos = [linha for linha in linhas if linha.get("evento") == "evento_consumido"]
    assert [linha["id"] for linha in consumidos] == [ok["id"]]
    (invalida,) = [linha for linha in linhas if linha.get("evento") == "evento_invalido"]
    assert invalida == {
        "evento": "evento_invalido",
        "consumidor": "antifraude",
        "message_id": "m-invalido",
        "erros": ["$.data.valor: minimum"],
    }
    assert "987.65" not in caplog.text
    (lote_log,) = [linha for linha in linhas if linha.get("msg") == "lote"]
    assert lote_log == {
        "msg": "lote",
        "consumidor": "antifraude",
        "CONSUMIDO": 1,
        "INVALIDO": 1,
        "DUPLICADO": 1,
        "EM_ANDAMENTO": 1,
    }


def test_falha_da_inbox_numa_mensagem_nao_derruba_o_resto_do_lote(
    instalar: Callable[[InboxEmMemoria], None],
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
) -> None:
    primeiro, problema, ultimo = cloudevent(), cloudevent(), cloudevent()
    inbox = InboxEspia(falhar_em=problema["id"])
    instalar(inbox)
    lote = {
        "Records": [
            _registro("m1", mensagem(primeiro)),
            _registro("m2", mensagem(problema)),
            _registro("m3", mensagem(ultimo)),
        ]
    }

    resposta = lambda_sqs.handler(lote, Contexto(30_000))

    assert resposta == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert {estado for estado, _ in inbox.itens.values()} == {"CONCLUIDO"}
    assert len(inbox.itens) == 2


def test_prazo_da_reivindicacao_e_o_fim_da_invocacao(
    instalar: Callable[[InboxEmMemoria], None],
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
) -> None:
    inbox = InboxEspia()
    instalar(inbox)

    lambda_sqs.handler({"Records": [_registro("m1", mensagem(cloudevent()))]}, Contexto(12_345))

    assert inbox.prazos == [AGORA_MS + 12_345]


def test_sem_contexto_do_lambda_o_prazo_e_o_timeout_padrao() -> None:
    assert lambda_sqs.prazo_da_invocacao_ms(None, AGORA_MS) == AGORA_MS + 30_000


def test_a_linha_emitida_e_json_numa_linha_de_log() -> None:
    linha = {"evento": "evento_consumido", "id": str(uuid.uuid4())}
    registros: list[logging.LogRecord] = []
    tratador = logging.Handler()
    tratador.emit = registros.append  # type: ignore[method-assign]
    lambda_sqs.log.addHandler(tratador)
    try:
        lambda_sqs._emitir(linha)
    finally:
        lambda_sqs.log.removeHandler(tratador)

    assert [json.loads(r.getMessage()) for r in registros] == [linha]


def test_composicao_le_o_ambiente_e_o_diretorio_de_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lambda_sqs, "_consumir", None)
    monkeypatch.setattr(lambda_sqs, "DIRETORIO_DOS_ESQUEMAS", RAIZ / "contracts" / "events")
    monkeypatch.setenv("CONSUMIDOR", "notificacoes")
    monkeypatch.setenv("TABELA_INBOX", "desafio-consumidor-notificacoes-inbox")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")

    consumir = lambda_sqs.consumir_evento()

    assert consumir.consumidor == "notificacoes"
    assert isinstance(consumir.validador, CatalogoDeEsquemas) and len(consumir.validador.tipos) == 8
    assert lambda_sqs.consumir_evento() is consumir  # uma vez por container


@pytest.mark.parametrize("faltando", ["CONSUMIDOR", "TABELA_INBOX", "esquemas"])
def test_configuracao_faltando_derruba_a_invocacao_inteira(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, faltando: str
) -> None:
    # Falha ALTA (Errors da função, alarme lambda-erros-<função>), não uma falha por mensagem que só a DLQ veria.
    monkeypatch.setattr(lambda_sqs, "_consumir", None)
    monkeypatch.setattr(lambda_sqs, "DIRETORIO_DOS_ESQUEMAS", RAIZ / "contracts" / "events")
    monkeypatch.setenv("CONSUMIDOR", "antifraude")
    monkeypatch.setenv("TABELA_INBOX", "desafio-consumidor-antifraude-inbox")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")
    if faltando == "esquemas":
        monkeypatch.setattr(lambda_sqs, "DIRETORIO_DOS_ESQUEMAS", tmp_path)
    else:
        monkeypatch.delenv(faltando)

    with pytest.raises((KeyError, FileNotFoundError)):
        lambda_sqs.handler({"Records": [_registro("m1", "{}")]}, Contexto(30_000))


def test_terraform_e_empacotamento_apontam_para_este_codigo() -> None:
    lambdas = (RAIZ / "infra/terraform/modules/lambdas/main.tf").read_text()
    script = (RAIZ / "scripts/empacotar_lambdas.sh").read_text()

    (handler,) = re.findall(r'handler\s*=\s*"(consumidores\.[\w.]+)"', lambdas)
    modulo, funcao = handler.rsplit(".", 1)
    assert modulo == lambda_sqs.__name__ and callable(getattr(lambda_sqs, funcao))
    for variavel in ("CONSUMIDOR", "TABELA_INBOX"):  # as que consumir_evento() lê
        assert re.search(rf"\b{variavel}\s*=", lambdas), variavel
    # o script copia os schemas para o diretório que o código lê no zip
    destino = lambda_sqs.DIRETORIO_DOS_ESQUEMAS.relative_to(
        Path(lambda_sqs.__file__).resolve().parents[1]
    )
    assert f"/{destino.as_posix()}" in script
