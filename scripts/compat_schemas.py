"""Gate de compatibilidade BACKWARD dos schemas de evento (ADR-0008).

Compara dois diretórios (`--base` = base do PR ou commit anterior do push, `--novo` = mudança).
Dentro do mesmo major (mesmo nome de arquivo `*.vN.schema.json`) só entram campos opcionais novos.
Sai com 1 se houver quebra.
Também é quebra todo APERTO de restrição: o schema novo passaria a recusar um evento que o da base
aceitava (reentrega, replay do archive, produtor ainda na versão anterior). Afrouxar não quebra.
Fail-closed: palavra-chave que o gate não sabe comparar (`if`/`then`/`else`, `propertyNames`, `contains`,
`dependentRequired`, `prefixItems`, `definitions`, qualquer uma de draft futuro) é quebra se nova, alterada ou
removida; trocar o `$schema` (o draft) é quebra, porque muda o que as outras palavras-chave significam.

Quebra aceita (`--aceitas`, ex.: contracts/events/quebras-aceitas.json): lista explícita e auditável de
quebras que o gate deixa passar, cada uma com schema, caminho, regra, motivo e data. Só casa a quebra
EXATA (`<schema>:<caminho>: <regra>`); qualquer outra continua reprovando. Arquivo inválido reprova.
Só stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

# Atributos CloudEvents cujo valor fixo identifica o major do evento: qualquer mudança quebra.
ATRIBUTOS_FIXOS = ("type", "source", "dataschema")

# Restrições textuais: nova ou alterada é quebra (não dá para decidir se uma regex contém a outra).
RESTRICOES_TEXTUAIS = ("pattern", "format")

# Combinadores e chaves estruturais: novo ou alterado é quebra. Em geral não dá para decidir se o novo aceita
# tudo o que a base aceitava (um `allOf` com `required`, um `not`, um `$ref` que troca o nó); remover só afrouxa.
ESTRUTURAIS = ("allOf", "anyOf", "oneOf", "not", "dependencies", "patternProperties", "$ref")

# Contagens: piso subindo ou teto descendo é aperto (mesma regra de minLength/maxLength).
CONTAGENS = (
    ("minLength", True),
    ("maxLength", False),
    ("minItems", True),
    ("maxItems", False),
    ("minProperties", True),
    ("maxProperties", False),
)

# Tudo o que o gate compara com regra própria. O que ficar fora (e fora de ANOTACOES) cai na regra fail-closed.
COMPARADAS = frozenset(
    {
        "$schema",
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "items",
        "additionalProperties",
        "uniqueItems",
        "multipleOf",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        *RESTRICOES_TEXTUAIS,
        *ESTRUTURAIS,
        *(chave for chave, _ in CONTAGENS),
    }
)

# Só documentação: não muda o que o schema aceita (a descrição de um campo pode mudar sem novo major).
ANOTACOES = frozenset(
    {
        "title",
        "description",
        "examples",
        "default",
        "$comment",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)

CAMPOS_DA_QUEBRA_ACEITA = ("schema", "caminho", "regra", "motivo", "data")


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

    for chave, piso in CONTAGENS:
        cb, cn = base.get(chave), novo.get(chave)
        if cn is not None and (cb is None or (cn > cb if piso else cn < cb)):
            quebras.append(f"{caminho}: {chave} apertado {cb} -> {cn}")

    if novo.get("uniqueItems") is True and base.get("uniqueItems") is not True:
        quebras.append(f"{caminho}: uniqueItems novo")

    for chave in ESTRUTURAIS:
        if chave in novo and novo[chave] != base.get(chave):
            quebras.append(f"{caminho}: {chave} novo ou alterado")

    # multipleOf novo, ou um que a base não satisfaz (0.01 -> 0.1 aperta; 0.01 -> 0.001 afrouxa).
    mb, mn = _numero(base.get("multipleOf")), _numero(novo.get("multipleOf"))
    if mn is not None and (mb is None or mb % mn != 0):
        quebras.append(f"{caminho}: multipleOf novo ou apertado {mb} -> {mn}")

    # Consumidor tolerante (ADR-0008): fechar o objeto a campos extras recusa evento com campo novo. Schema
    # contra schema compara recursivamente (_comparar_subesquemas).
    ab, an = base.get("additionalProperties", True), novo.get("additionalProperties", True)
    if (an is False and ab is not False) or (isinstance(an, dict) and ab is True):
        quebras.append(f"{caminho}: additionalProperties restringido {ab!r} -> {an!r}")


def _comparar_sem_regra(
    base: dict[str, Any], novo: dict[str, Any], caminho: str, quebras: list[str]
) -> None:
    """`$schema` e palavras-chave fora de COMPARADAS/ANOTACOES: qualquer mudança é quebra (fail-closed)."""
    # O draft muda o significado das outras palavras-chave (draft-04 ignora `if`/`propertyNames`; o 2020-12 lê
    # `items` de outro jeito): trocar, criar ou tirar o `$schema` é quebra.
    if base.get("$schema") != novo.get("$schema"):
        quebras.append(
            f"{caminho}: $schema alterado {base.get('$schema')!r} -> {novo.get('$schema')!r}"
        )

    # Fail-closed: o gate não sabe se a mudança aperta, então qualquer mudança quebra, inclusive tirar (sem o
    # `prefixItems`, o `items` do 2020-12 passa a valer desde a posição 0; sem um `minContains: 0`, o `contains`
    # passa a exigir um item).
    for chave in sorted((base.keys() | novo.keys()) - COMPARADAS - ANOTACOES):
        if base.get(chave) != novo.get(chave):
            quebras.append(
                f"{caminho}: {chave} novo, alterado ou removido (palavra-chave sem comparação no gate)"
            )


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
    # enum/const criado: o nó aceitava qualquer valor do tipo e agora só uma lista fechada.
    if vb is None and vn is not None:
        quebras.append(f"{caminho}: enum/const novo {sorted(vn)}")
    elif vb is not None:
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
    _comparar_sem_regra(base, novo, caminho, quebras)

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

    _comparar_subesquemas(base, novo, caminho, quebras, avisos)


def _comparar_subesquemas(
    base: dict[str, Any], novo: dict[str, Any], caminho: str, quebras: list[str], avisos: list[str]
) -> None:
    """Os nós que têm schema dentro, fora `properties`: `items` e `additionalProperties`."""
    # `items` novo compara contra o schema vazio (que aceita tudo): qualquer restrição nele é aperto.
    if isinstance(novo.get("items"), dict) and isinstance(base.get("items", {}), dict):
        comparar_no(base.get("items", {}), novo["items"], f"{caminho}[]", quebras, avisos)
    # `items` em lista (tupla, draft-04 a 07) valida por posição: o gate não compara lista, então qualquer
    # mudança que envolva lista (nova, alterada, ou trocada por schema único) é quebra.
    ib, inovo = base.get("items"), novo.get("items")
    if (isinstance(ib, list) or isinstance(inovo, list)) and ib != inovo:
        quebras.append(f"{caminho}: items em lista (tupla) novo, alterado ou removido")

    # additionalProperties schema contra schema: apertar o schema dos campos extras é aperto.
    ab, an = base.get("additionalProperties"), novo.get("additionalProperties")
    if isinstance(ab, dict) and isinstance(an, dict):
        comparar_no(ab, an, f"{caminho}{{*}}", quebras, avisos)


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


def carregar_aceitas(arquivo: Path) -> list[dict[str, str]]:
    """Lê e valida o registro de quebras aceitas. Qualquer defeito levanta ValueError (o gate reprova)."""
    try:
        conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as erro:
        raise ValueError(f"{arquivo}: ilegível ({erro})") from erro
    entradas = conteudo.get("quebras") if isinstance(conteudo, dict) else None
    if not isinstance(entradas, list):
        raise ValueError(f"{arquivo}: esperado objeto com a lista `quebras`")
    for n, entrada in enumerate(entradas):
        onde = f"{arquivo}: quebras[{n}]"
        if not isinstance(entrada, dict) or set(entrada) != set(CAMPOS_DA_QUEBRA_ACEITA):
            raise ValueError(f"{onde}: campos devem ser exatamente {list(CAMPOS_DA_QUEBRA_ACEITA)}")
        if not all(isinstance(v, str) and v.strip() for v in entrada.values()):
            raise ValueError(f"{onde}: todo campo é texto não vazio")
        if not entrada["schema"].endswith(".schema.json") or not entrada["caminho"].startswith("$"):
            raise ValueError(f"{onde}: `schema` é um *.schema.json e `caminho` começa em `$`")
        try:
            date.fromisoformat(entrada["data"])
        except ValueError as erro:
            raise ValueError(f"{onde}: `data` não é AAAA-MM-DD") from erro
    return entradas


def separar_aceitas(
    quebras: list[str], aceitas: list[dict[str, str]]
) -> tuple[list[str], list[str], list[str]]:
    """(quebras que reprovam, quebras aceitas com o motivo, entradas sem efeito). Casamento EXATO."""
    por_texto = {f"{a['schema']}:{a['caminho']}: {a['regra']}": a for a in aceitas}
    restantes = [q for q in quebras if q not in por_texto]
    usadas = [
        f"{q} (motivo: {por_texto[q]['motivo']}; {por_texto[q]['data']})"
        for q in quebras
        if q in por_texto
    ]
    sem_efeito = [texto for texto in por_texto if texto not in quebras]
    return restantes, usadas, sem_efeito


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="schemas da base")
    parser.add_argument("--novo", type=Path, required=True, help="schemas da mudança")
    parser.add_argument("--aceitas", type=Path, help="registro de quebras aceitas (JSON)")
    args = parser.parse_args(argv)

    try:
        aceitas = carregar_aceitas(args.aceitas) if args.aceitas else []
    except ValueError as erro:
        print(f"ERRO   {erro}")
        return 2
    quebras, avisos = comparar_diretorios(args.base, args.novo)
    quebras, usadas, sem_efeito = separar_aceitas(quebras, aceitas)
    for aviso in avisos:
        print(f"AVISO  {aviso}")
    for texto in sem_efeito:
        print(
            f"AVISO  quebra aceita sem efeito (a base já a contém?), pode sair do registro: {texto}"
        )
    for texto in usadas:
        print(f"ACEITA {texto}")
    for quebra in quebras:
        print(f"QUEBRA {quebra}")
    if quebras:
        print(f"\n{len(quebras)} quebra(s) BACKWARD. Mudança incompatível exige novo major (v2).")
        return 1
    if usadas:
        print(f"schemas compatíveis (BACKWARD), com {len(usadas)} quebra(s) aceita(s) no registro")
    else:
        print("schemas compatíveis (BACKWARD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
