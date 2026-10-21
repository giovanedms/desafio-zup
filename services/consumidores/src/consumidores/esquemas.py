"""Catálogo dos schemas de evento e validação do CloudEvent contra o schema do seu `type`.

Os schemas são os de `contracts/events/*.schema.json` (o contrato publicado, ADR-0008), não uma cópia mantida à mão:
no zip da Lambda, `scripts/empacotar_lambdas.sh` copia os arquivos para `consumidores/esquemas/`; nos testes, o
catálogo lê o diretório do repositório. O validador é o do `$schema` de cada arquivo (draft-04, o mesmo da esteira
nos exemplos), com dois ajustes ao que chega da fila:
- `date-time` conferido de verdade: sem o pacote opcional `rfc3339-validator`, o `jsonschema` aceita qualquer texto
  nesse formato, e `time`/`occurred_at` são o que o destino usa para ordenar;
- `integer` = número sem parte fracionária: o EventBridge reserializa os números do `detail` como double (ADR-0011
  §4: `1.00` chega `1.0`), e um inteiro que chegue como `42.0` não pode virar evento inválido. `42.5` continua sendo.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker, ValidationError, validators
from jsonschema.protocols import Validator

from consumidores.nucleo import EventoInvalido

# RFC 3339, seção 5.6 (date-time). O `fromisoformat` abaixo confere as faixas (mês 13, 30 de fevereiro); ele recusa o
# segundo bissexto (:60), que os produtores não emitem.
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", re.ASCII
)


def _data_hora(valor: object) -> bool:
    if not isinstance(valor, str):
        return True  # `format` só restringe string; o `type` do schema cuida do resto
    if not _RFC3339.fullmatch(valor):
        return False
    datetime.fromisoformat(valor.upper())  # ValueError fora de faixa
    return True


# Instância própria: o `checks` altera só ela, não o FormatChecker global do jsonschema.
_FORMATOS = FormatChecker()
_FORMATOS.checks("date-time", raises=ValueError)(_data_hora)


def _inteiro(checker: object, valor: object) -> bool:
    if isinstance(valor, bool):
        return False
    if isinstance(valor, int):
        return True
    if isinstance(valor, Decimal):
        return valor.is_finite() and valor == valor.to_integral_value()
    return isinstance(valor, float) and math.isfinite(valor) and valor.is_integer()


class CatalogoDeEsquemas:
    """CE `type` → validador do schema daquele tipo. Implementa a porta `Validador` do núcleo."""

    def __init__(self, esquemas: Mapping[str, Mapping[str, Any]]) -> None:
        if not esquemas:
            raise ValueError("catálogo de schemas vazio")
        self._validadores: dict[str, Validator] = {}
        for tipo, esquema in esquemas.items():
            classe = validators.validator_for(esquema, default=None)
            if classe is None:
                raise ValueError(f"schema de {tipo} sem $schema conhecido")
            # schema quebrado derruba a carga, não a validação de cada evento
            classe.check_schema(esquema)
            tolerante = validators.extend(
                classe, type_checker=classe.TYPE_CHECKER.redefine("integer", _inteiro)
            )
            self._validadores[tipo] = tolerante(esquema, format_checker=_FORMATOS)

    @classmethod
    def do_diretorio(cls, diretorio: Path) -> CatalogoDeEsquemas:
        """Um `*.schema.json` por tipo; o tipo vem do próprio schema (o enum de valor único em `properties.type`)."""
        esquemas: dict[str, Mapping[str, Any]] = {}
        for arquivo in sorted(diretorio.glob("*.schema.json")):
            esquema = json.loads(arquivo.read_text(encoding="utf-8"))
            tipo = _tipo_fixado(esquema, arquivo.name)
            if tipo in esquemas:
                raise ValueError(f"dois schemas para o tipo {tipo}")
            esquemas[tipo] = esquema
        if not esquemas:
            raise FileNotFoundError(f"nenhum schema de evento em {diretorio}")
        return cls(esquemas)

    @property
    def tipos(self) -> frozenset[str]:
        return frozenset(self._validadores)

    def validar(self, cloudevent: Mapping[str, Any]) -> None:
        tipo = cloudevent.get("type")
        validador = self._validadores.get(tipo) if isinstance(tipo, str) else None
        if validador is None:
            # Tipo sem schema aqui (outro major, ou regra que passou a rotear um tipo novo): recusar, não adivinhar.
            raise EventoInvalido("$.type: sem schema no catálogo")
        erros = sorted({_descrever(e) for e in validador.iter_errors(cloudevent)})
        if erros:
            raise EventoInvalido(*erros)


def _tipo_fixado(esquema: Mapping[str, Any], nome: str) -> str:
    try:
        (tipo,) = esquema["properties"]["type"]["enum"]
    except (KeyError, TypeError, ValueError) as erro:
        raise ValueError(
            f"{nome}: o schema precisa fixar o `type` do CloudEvent num enum de um valor"
        ) from erro
    if not isinstance(tipo, str):
        raise ValueError(f"{nome}: `type` fixado não é texto")
    return tipo


def _descrever(erro: ValidationError) -> str:
    """Caminho e palavra-chave, sem o valor (`$.data.valor: minimum`). A mensagem do jsonschema traz o valor recebido
    ("Decimal('-1.5') is less than..."), por isso não é usada. Em `required`, os campos que faltam (nomes do schema)."""
    if erro.validator == "required" and isinstance(erro.instance, dict):
        faltando = [c for c in erro.validator_value if c not in erro.instance]
        return f"{erro.json_path}: required {','.join(faltando)}"
    return f"{erro.json_path}: {erro.validator}"
