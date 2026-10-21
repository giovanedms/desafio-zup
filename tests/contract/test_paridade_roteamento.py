"""Paridade de roteamento: as regras do bus que a demo cria (`infra/local/bootstrap.py`) são as que o Terraform
implanta (`infra/terraform/modules/eventos`). Os dois arquivos afirmam ser iguais; este teste é quem garante.

O bootstrap roda de verdade contra clientes falsos que só anotam `put_rule`/`put_targets`; o HCL é lido por regex
(o `terraform fmt` da esteira deixa o formato canônico). Nenhuma chamada à AWS nem à LocalStack.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from relay.cdc_limites import SOURCE_NO_BUS
from relay.eventbridge import SOURCE_PADRAO

RAIZ = Path(__file__).resolve().parents[2]
MODULO = RAIZ / "infra/terraform/modules/eventos"
Regras = dict[
    str, tuple[str, dict[str, list[str]], list[str]]
]  # nome → (bus, padrão, filas de destino)


def _carregar_bootstrap() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bootstrap_paridade", RAIZ / "infra/local/bootstrap.py"
    )
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


bootstrap = _carregar_bootstrap()


class EventsFalso:
    def __init__(self) -> None:
        self.regras: dict[str, tuple[str, dict[str, list[str]]]] = {}
        self.alvos: dict[str, list[str]] = {}
        self.exceptions = MagicMock()

    def create_event_bus(self, **_: Any) -> None:
        return None

    def put_rule(self, Name: str, EventBusName: str, EventPattern: str, **_: Any) -> None:  # noqa: N803
        self.regras[Name] = (EventBusName, json.loads(EventPattern))

    def put_targets(self, Rule: str, EventBusName: str, Targets: list[dict[str, str]]) -> None:  # noqa: N803
        self.alvos[Rule] = [t["Arn"].removeprefix("arn:fila:") for t in Targets]


class SqsFalso:
    def create_queue(self, QueueName: str, **_: Any) -> dict[str, str]:  # noqa: N803
        return {"QueueUrl": f"url:{QueueName}"}

    def get_queue_url(self, QueueName: str) -> dict[str, str]:  # noqa: N803
        return {"QueueUrl": f"url:{QueueName}"}

    def get_queue_attributes(self, QueueUrl: str, **_: Any) -> dict[str, dict[str, str]]:  # noqa: N803
        return {"Attributes": {"QueueArn": f"arn:fila:{QueueUrl.removeprefix('url:')}"}}


@pytest.fixture
def regras_do_bootstrap(monkeypatch: pytest.MonkeyPatch) -> Regras:
    events = EventsFalso()
    clientes = {"dynamodb": MagicMock(), "sqs": SqsFalso(), "events": events}
    monkeypatch.setattr(bootstrap, "cliente", lambda servico: clientes[servico])
    monkeypatch.setattr(bootstrap, "criar_tabela", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "RepositorioDeLimitesDynamoDB", MagicMock())
    bootstrap.main()
    return {
        nome: (bus, padrao, events.alvos[nome]) for nome, (bus, padrao) in events.regras.items()
    }


def _hcl(arquivo: str) -> str:
    return (MODULO / arquivo).read_text(encoding="utf-8")


def _default_texto(variaveis: str, nome: str) -> str:
    achado = re.search(rf'variable "{nome}" \{{.*?default\s*=\s*"([^"]*)"', variaveis, re.DOTALL)
    assert achado, nome
    return achado.group(1)


def _assinaturas(variaveis: str) -> dict[str, list[str]]:
    bloco = re.search(
        r'variable "assinaturas" \{.*?default\s*=\s*\{(.*?)\n  \}', variaveis, re.DOTALL
    )
    assert bloco, "default de assinaturas não encontrado"
    pares = re.findall(r"(\w+)\s*=\s*\[([^\]]*)\]", bloco.group(1))
    return {
        nome: json.loads("[" + re.sub(r",\s*$", "", lista.strip()) + "]") for nome, lista in pares
    }


def _bloco(main: str, recurso: str, nome: str) -> str:
    achado = re.search(rf'resource "{recurso}" "{nome}" \{{\n(.*?)\n\}}', main, re.DOTALL)
    assert achado, f"{recurso}.{nome}"
    return achado.group(1)


def _regras_do_terraform() -> Regras:
    variaveis, main = _hcl("variables.tf"), _hcl("main.tf")
    bus = _default_texto(variaveis, "nome_bus")
    consumidor = _default_texto(variaveis, "consumidor_limite_alterado")

    # Formato das regras e dos alvos no HCL: nome, padrão e fila de destino (a fila se chama each.key).
    por_consumidor = _bloco(main, "aws_cloudwatch_event_rule", "this")
    for trecho in (
        "for_each = var.assinaturas",
        'name           = "para-${each.key}"',
        "source        = [var.source_eventos]",
        '"detail-type" = each.value',
    ):
        assert trecho in por_consumidor, trecho
    assert "arn            = aws_sqs_queue.this[each.key].arn" in _bloco(
        main, "aws_cloudwatch_event_target", "this"
    )
    assert "name                       = each.key" in _bloco(main, "aws_sqs_queue", "this")
    do_limite = _bloco(main, "aws_cloudwatch_event_rule", "limite_alterado")
    for trecho in (
        'name           = "limite-alterado-para-${var.consumidor_limite_alterado}"',
        "source        = [var.source_limites]",
        '"detail-type" = ["LimiteAlterado"]',
    ):
        assert trecho in do_limite, trecho
    assert "arn            = aws_sqs_queue.this[var.consumidor_limite_alterado].arn" in _bloco(
        main, "aws_cloudwatch_event_target", "limite_alterado"
    )

    fonte = _default_texto(variaveis, "source_eventos")
    regras: Regras = {
        f"para-{nome}": (bus, {"source": [fonte], "detail-type": tipos}, [nome])
        for nome, tipos in _assinaturas(variaveis).items()
    }
    regras[f"limite-alterado-para-{consumidor}"] = (
        bus,
        {
            "source": [_default_texto(variaveis, "source_limites")],
            "detail-type": ["LimiteAlterado"],
        },
        [consumidor],
    )
    return regras


def test_bootstrap_e_terraform_criam_as_mesmas_regras(regras_do_bootstrap: Regras) -> None:
    assert regras_do_bootstrap == _regras_do_terraform()


def test_o_ambiente_usa_os_defaults_do_modulo() -> None:
    """A paridade é com o `default`: nenhum ambiente pode sobrescrever o roteamento sem este teste ver."""
    sobrescritas = (
        "assinaturas",
        "source_eventos",
        "source_limites",
        "consumidor_limite_alterado",
        "nome_bus",
    )
    for main in (RAIZ / "infra/terraform/environments").glob("*/main.tf"):
        bloco = re.search(
            r'module "eventos" \{\n(.*?)\n\}', main.read_text(encoding="utf-8"), re.DOTALL
        )
        if bloco:
            usadas = {
                v for v in sobrescritas if re.search(rf"^\s*{v}\s*=", bloco.group(1), re.MULTILINE)
            }
            assert not usadas, f"{main}: {usadas}"


def test_sources_das_regras_sao_os_que_o_relay_publica() -> None:
    variaveis = _hcl("variables.tf")
    assert _default_texto(variaveis, "source_eventos") == bootstrap.SOURCE_DO_BUS == SOURCE_PADRAO
    assert (
        _default_texto(variaveis, "source_limites") == bootstrap.SOURCE_DOS_LIMITES == SOURCE_NO_BUS
    )


def test_todo_detail_type_assinado_existe_no_contrato() -> None:
    """Erro de digitação num detail-type não dá erro nenhum: a regra só nunca casa. O contrato é a lista."""
    publicados = set()
    for schema in (RAIZ / "contracts/events").glob("*.schema.json"):
        dados = json.loads(schema.read_text(encoding="utf-8"))["properties"]["data"]["properties"]
        publicados |= set(dados["event_type"]["enum"])
    assinados = {t for tipos in _assinaturas(_hcl("variables.tf")).values() for t in tipos}
    assert assinados | {"LimiteAlterado"} <= publicados
