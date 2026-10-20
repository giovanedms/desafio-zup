"""Gate de compatibilidade BACKWARD dos schemas de evento (ADR-0008).

Compara dois diretórios (`--base` = base do PR ou commit anterior do push, `--novo` = mudança).
Dentro do mesmo major (mesmo nome de arquivo `*.vN.schema.json`) só entram campos opcionais novos.
Sai com 1 se houver quebra.
Também é quebra todo APERTO de restrição: o schema novo passaria a recusar um evento que o da base
aceitava (reentrega, replay do archive, produtor ainda na versão anterior). Afrouxar não quebra.
Só stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# Atributos CloudEvents cujo valor fixo identifica o major do evento: qualquer mudança quebra.
ATRIBUTOS_FIXOS = ("type", "source", "dataschema")

# Restrições textuais: nova ou alterada é quebra (não dá para decidir se uma regex contém a outra).
RESTRICOES_TEXTUAIS = ("pattern", "format")


def _tipos(no: dict[str, Any]) -> set[str]:
    tipo = no.get("type")
    if tipo is None:
        return set()
    return {tipo} if isinstance(tipo, str) else set(tipo)


def _valores_fixos(no: dict[str, Any]) -> set[str] | None:
    if "const" in no:
        return {json.dumps(no["const"])}
    if "enum" in no:
        return {json.dumps(v) for v in no["enum"]}
    return None


def _numero(valor: Any) -> Decimal | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    return Decimal(str(valor))


def _limite(
    no: dict[str, Any], chave: str, chave_exclusiva: str, piso: bool
) -> tuple[Decimal, bool] | None:
    """Limite numérico efetivo (valor, exclusivo). draft-04: `exclusiveMinimum` booleano qualifica o
    `minimum`; draft-06+: `exclusiveMinimum` numérico é o próprio limite. Com os dois, vale o mais apertado."""
    candidatos: list[tuple[Decimal, bool]] = []
    valor, exclusivo = _numero(no.get(chave)), no.get(chave_exclusiva)
    if valor is not None:
        candidatos.append((valor, exclusivo is True))
    if (numero_exclusivo := _numero(exclusivo)) is not None:
        candidatos.append((numero_exclusivo, True))
    if not candidatos:
        return None
    if piso:  # o maior valor; no empate, o exclusivo
        return max(candidatos)
    # teto: o menor valor; no empate, o exclusivo
    return min(candidatos, key=lambda c: (c[0], not c[1]))


def _texto(limite: tuple[Decimal, bool] | None, piso: bool) -> str:
    if limite is None:
        return "nenhum"
    operador = (">" if piso else "<") + ("" if limite[1] else "=")
    return f"{operador} {limite[0]}"


def _apertou_limite(
    base: tuple[Decimal, bool] | None, novo: tuple[Decimal, bool] | None, piso: bool
) -> bool:
    if novo is None:
        return False
    if base is None:
        return True
    if novo[0] != base[0]:
        return novo[0] > base[0] if piso else novo[0] < base[0]
    return novo[1] and not base[1]


def _comparar_restricoes(
    base: dict[str, Any], novo: dict[str, Any], caminho: str, quebras: list[str]
) -> None:
    """Aperto de restrição de valor (o schema novo recusaria dado que o da base aceitava)."""
    for chave in RESTRICOES_TEXTUAIS:
        if chave in novo and novo[chave] != base.get(chave):
            quebras.append(
                f"{caminho}: {chave} novo ou alterado {base.get(chave)!r} -> {novo[chave]!r}"
            )

    for chave, exclusiva, piso in (
        ("minimum", "exclusiveMinimum", True),
        ("maximum", "exclusiveMaximum", False),
    ):
        lb, ln = _limite(base, chave, exclusiva, piso), _limite(novo, chave, exclusiva, piso)
        if _apertou_limite(lb, ln, piso):
            quebras.append(
                f"{caminho}: {chave}/{exclusiva} apertado {_texto(lb, piso)} -> {_texto(ln, piso)}"
            )

    for chave, piso in (("minLength", True), ("maxLength", False)):
        cb, cn = base.get(chave), novo.get(chave)
        if cn is not None and (cb is None or (cn > cb if piso else cn < cb)):
            quebras.append(f"{caminho}: {chave} apertado {cb} -> {cn}")

    # multipleOf novo, ou um que a base não satisfaz (0.01 -> 0.1 aperta; 0.01 -> 0.001 afrouxa).
    mb, mn = _numero(base.get("multipleOf")), _numero(novo.get("multipleOf"))
    if mn is not None and (mb is None or mb % mn != 0):
        quebras.append(f"{caminho}: multipleOf novo ou apertado {mb} -> {mn}")

    # Consumidor tolerante (ADR-0008): fechar o objeto a campos extras recusa evento com campo novo.
    ab, an = base.get("additionalProperties", True), novo.get("additionalProperties", True)
    if (an is False and ab is not False) or (isinstance(an, dict) and ab is True):
        quebras.append(f"{caminho}: additionalProperties restringido {ab!r} -> {an!r}")


def comparar_no(
    base: dict[str, Any],
    novo: dict[str, Any],
    caminho: str,
    quebras: list[str],
    avisos: list[str],
    *,
    fixo: bool = False,
) -> None:
    """Compara recursivamente dois nós de schema acumulando quebras e avisos."""
    if _tipos(base) != _tipos(novo) and (_tipos(base) or _tipos(novo)):
        quebras.append(f"{caminho}: type alterado {sorted(_tipos(base))} -> {sorted(_tipos(novo))}")

    vb, vn = _valores_fixos(base), _valores_fixos(novo)
    if vb is not None:
        if vn is None:
            quebras.append(f"{caminho}: enum/const removido")
        elif fixo and vb != vn:
            quebras.append(f"{caminho}: valor fixo alterado {sorted(vb)} -> {sorted(vn)}")
        else:
            for v in sorted(vb - vn):
                quebras.append(f"{caminho}: valor removido de enum: {v}")
            for v in sorted(vn - vb):
                avisos.append(f"{caminho}: valor adicionado a enum: {v}")

    for campo in sorted(set(novo.get("required", [])) - set(base.get("required", []))):
        quebras.append(f"{caminho}.{campo}: campo novo em required")

    _comparar_restricoes(base, novo, caminho, quebras)

    props_base = base.get("properties", {})
    props_novo = novo.get("properties", {})
    for nome, sub_base in props_base.items():
        destino = f"{caminho}.{nome}"
        if nome not in props_novo:
            quebras.append(f"{destino}: propriedade removida")
            continue
        comparar_no(
            sub_base,
            props_novo[nome],
            destino,
            quebras,
            avisos,
            fixo=caminho.endswith("$") and nome in ATRIBUTOS_FIXOS,
        )

    if isinstance(base.get("items"), dict) and isinstance(novo.get("items"), dict):
        comparar_no(base["items"], novo["items"], f"{caminho}[]", quebras, avisos)


def comparar_diretorios(base: Path, novo: Path) -> tuple[list[str], list[str]]:
    quebras: list[str] = []
    avisos: list[str] = []
    for arquivo in sorted(base.glob("*.schema.json")):
        alvo = novo / arquivo.name
        if not alvo.exists():
            quebras.append(f"{arquivo.name}: arquivo de schema removido")
            continue
        comparar_no(
            json.loads(arquivo.read_text(encoding="utf-8")),
            json.loads(alvo.read_text(encoding="utf-8")),
            f"{arquivo.name}:$",
            quebras,
            avisos,
        )
    return quebras, avisos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="schemas da base")
    parser.add_argument("--novo", type=Path, required=True, help="schemas da mudança")
    args = parser.parse_args(argv)

    quebras, avisos = comparar_diretorios(args.base, args.novo)
    for aviso in avisos:
        print(f"AVISO  {aviso}")
    for quebra in quebras:
        print(f"QUEBRA {quebra}")
    if quebras:
        print(f"\n{len(quebras)} quebra(s) BACKWARD. Mudança incompatível exige novo major (v2).")
        return 1
    print("schemas compatíveis (BACKWARD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
