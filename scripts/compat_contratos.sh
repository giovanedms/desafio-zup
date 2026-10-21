#!/usr/bin/env bash
# Gate de compatibilidade de contratos contra um ref git base (default origin/main).
# (a) schemas de evento: scripts/compat_schemas.py; (b) OpenAPI: oasdiff breaking (via docker).
# Uso: scripts/compat_contratos.sh [REF]
#
# Fail-closed na CI (CI=true, que o GitHub Actions define): base inexistente ou docker indisponível
# FALHAM, porque um gate que não roda não pode passar. Local (`make compat`), os mesmos casos só avisam.
# REF = SHA zero (push que cria a branch; `github.event.before`): não há commit anterior. Compara com
# origin/main; se nem ela existe (primeiro push do repositório), compara o HEAD com ele mesmo e diz isso.
set -euo pipefail

REF="${1:-origin/main}"
RAIZ="$(git rev-parse --show-toplevel)"
OASDIFF_IMAGE="tufin/oasdiff:v1.11.7"
SHA_ZERO="0000000000000000000000000000000000000000"
cd "$RAIZ"

# sem_gate <motivo>: na CI é falha; local, aviso explícito de que o gate não rodou por inteiro.
sem_gate() {
  if [ "${CI:-}" = "true" ]; then
    echo "ERRO: $1 (CI: fail-closed)" >&2
    exit 1
  fi
  echo "AVISO: $1 (local: segue sem falhar; na CI isto reprova)"
  exit 0
}

if [ "$REF" = "$SHA_ZERO" ]; then
  if git rev-parse --verify --quiet "origin/main^{commit}" >/dev/null; then
    echo "branch nova (sem commit anterior): comparando com origin/main"
    REF="origin/main"
  else
    echo "primeiro push do repositório (sem origin/main): comparando HEAD com ele mesmo"
    REF="HEAD"
  fi
fi

if ! git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null; then
  sem_gate "ref base '${REF}' não existe neste clone (fetch-depth? force-push?)"
fi

BASE_DIR="$(mktemp -d)"
trap 'rm -rf "$BASE_DIR"' EXIT

# contracts/ ausente no ref base (a mudança que o cria): não há contrato anterior a quebrar.
if ! git cat-file -e "${REF}:contracts" 2>/dev/null; then
  echo "sem contratos na base '${REF}': nada a comparar"
  exit 0
fi
git archive "$REF" contracts | tar -x -C "$BASE_DIR"

falhou=0

echo "== schemas de evento (${REF} -> working tree) =="
# Quebras aceitas: registro explícito e versionado (ADR-0008); arquivo ausente ou inválido reprova.
python3 scripts/compat_schemas.py --base "$BASE_DIR/contracts/events" --novo contracts/events \
  --aceitas contracts/events/quebras-aceitas.json || falhou=1

echo "== OpenAPI (oasdiff ${OASDIFF_IMAGE}) =="
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  [ "$falhou" -eq 0 ] || exit 1  # a quebra de schema já reprovou; não esconder atrás do aviso
  sem_gate "docker indisponível, oasdiff não rodou"
fi
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

exit "$falhou"
