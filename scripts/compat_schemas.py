"""Gate de compatibilidade BACKWARD dos schemas de evento (ADR-0008).

Compara dois diretórios (`--base` = main, `--novo` = PR). Dentro do mesmo major (mesmo nome de
arquivo `*.vN.schema.json`) só entram campos opcionais novos. Sai com 1 se houver quebra.
Só stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Atributos CloudEvents cujo valor fixo identifica o major do evento: qualquer mudança quebra.
ATRIBUTOS_FIXOS = ("type", "source", "dataschema")


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
    parser.add_argument("--base", type=Path, required=True, help="schemas da main")
    parser.add_argument("--novo", type=Path, required=True, help="schemas do PR")
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
