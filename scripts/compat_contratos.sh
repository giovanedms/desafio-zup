#!/usr/bin/env bash
# Gate de compatibilidade de contratos contra um ref git base (default origin/main).
# (a) schemas de evento: scripts/compat_schemas.py; (b) OpenAPI: oasdiff breaking (via docker).
# Uso: scripts/compat_contratos.sh [REF]
set -euo pipefail

REF="${1:-origin/main}"
RAIZ="$(git rev-parse --show-toplevel)"
OASDIFF_IMAGE="tufin/oasdiff:v1.11.7"
cd "$RAIZ"

if ! git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null; then
  echo "sem base para comparar (ref '${REF}' não existe)"
  exit 0
fi

BASE_DIR="$(mktemp -d)"
trap 'rm -rf "$BASE_DIR"' EXIT

# contracts/ pode não existir no ref base (primeiro PR que o cria): nada a comparar.
if ! git archive "$REF" contracts 2>/dev/null | tar -x -C "$BASE_DIR"; then
  echo "sem base para comparar (contracts/ ausente em '${REF}')"
  exit 0
fi

falhou=0

echo "== schemas de evento (${REF} -> working tree) =="
python3 scripts/compat_schemas.py --base "$BASE_DIR/contracts/events" --novo contracts/events || falhou=1

echo "== OpenAPI (oasdiff ${OASDIFF_IMAGE}) =="
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "AVISO: docker indisponível, oasdiff pulado"
else
  for novo in contracts/openapi/*.yaml; do
    nome="$(basename "$novo")"
    if [ ! -f "$BASE_DIR/contracts/openapi/$nome" ]; then
      echo "$nome: novo na base, sem comparação"
      continue
    fi
    echo "-- $nome"
    docker run --rm -v "$BASE_DIR/contracts:/base:ro" -v "$RAIZ/contracts:/novo:ro" \
      "$OASDIFF_IMAGE" breaking "/base/openapi/$nome" "/novo/openapi/$nome" --fail-on ERR || falhou=1
  done
fi

exit "$falhou"
