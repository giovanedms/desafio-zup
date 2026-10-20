#!/usr/bin/env bash
# Gera os zips das Lambdas (arm64, Python 3.13) em infra/terraform/build/.
#   relay.zip           -> pacote `relay` (relay + sweeper)
#   contabilizacao.zip  -> pacote `contabilizacao`
# Dependências são resolvidas do uv.lock (--frozen) e instaladas para a plataforma do Lambda
# (aarch64-manylinux2014), independentemente da máquina que roda o script. Idempotente: recria os zips.
# Requer: uv, zip.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAIDA="${RAIZ}/infra/terraform/build"
PYTHON_VERSION="3.13"
PLATAFORMA="aarch64-manylinux2014"

for ferramenta in uv zip; do
  command -v "${ferramenta}" >/dev/null || { echo "erro: '${ferramenta}' não encontrado no PATH" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${SAIDA}"

# empacotar <pacote-do-workspace>
empacotar() {
  local pacote="$1"
  local dir="${TMP}/${pacote}"
  local zip_final="${SAIDA}/${pacote}.zip"
  mkdir -p "${dir}"

  echo ">> ${pacote}: exportando dependências (uv.lock)"
  # --no-emit-workspace: membros do workspace (o próprio pacote) entram abaixo, não editáveis.
  (cd "${RAIZ}" && uv export --quiet --package "${pacote}" --no-dev --frozen --no-hashes --no-emit-workspace \
    --no-header -o "${TMP}/${pacote}.requirements.txt")

  echo ">> ${pacote}: instalando para ${PLATAFORMA} / py${PYTHON_VERSION}"
  # boto3 já vem no runtime do Lambda, mas fixamos a versão do lock para reprodutibilidade.
  uv pip install --quiet --target "${dir}" --python-platform "${PLATAFORMA}" \
    --python-version "${PYTHON_VERSION}" --no-deps -r "${TMP}/${pacote}.requirements.txt"

  echo ">> ${pacote}: instalando o código do workspace"
  # O próprio pacote como pacotes normais, sem dependências (já instaladas acima).
  (cd "${RAIZ}" && uv pip install --quiet --target "${dir}" --python-platform "${PLATAFORMA}" \
    --python-version "${PYTHON_VERSION}" --no-deps "$(caminho_do_pacote "${pacote}")")

  find "${dir}" -type d -name '__pycache__' -prune -exec rm -rf {} +
  rm -f "${zip_final}"
  (cd "${dir}" && zip --quiet --recurse-paths --symlinks "${zip_final}" .)
  echo ">> ${pacote}: ${zip_final} ($(du -h "${zip_final}" | cut -f1))"
}

# Caminho de cada membro do workspace (o "./" é obrigatório: sem ele o uv trata como nome de pacote do PyPI).
caminho_do_pacote() {
  case "$1" in
    relay) echo "./relay" ;;
    contabilizacao) echo "./services/contabilizacao" ;;
    *) echo "erro: pacote desconhecido '$1'" >&2; return 1 ;;
  esac
}

empacotar relay
empacotar contabilizacao
