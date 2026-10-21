# syntax=docker/dockerfile:1
# Build a partir da RAIZ do workspace (context = entrega/):
#   docker build -f services/contabilizacao/Dockerfile -t desafio-zup/contabilizacao:dev .

# Bases fixadas por DIGEST (tag é mutável; o digest, não). A tag fica para leitura e para o Dependabot, que abre PR
# quando ela anda. Resolvidas em 21/10/2026: uv 0.12.24 e Python 3.13.16 (Debian trixie), índices multi-arquitetura.
# Fonte do binário do uv.
FROM ghcr.io/astral-sh/uv:0.13@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

# ---- build: resolve e instala só o necessário para o serviço contabilizacao num venv isolado ----
FROM python:3.13-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS build
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Manifestos primeiro: a camada de dependências só é refeita quando pyproject/uv.lock mudam.
# O uv exige o pyproject de TODOS os membros do workspace para ler o uv.lock.
COPY pyproject.toml uv.lock ./
COPY services/autorizacao/pyproject.toml services/autorizacao/pyproject.toml
COPY services/limites/pyproject.toml services/limites/pyproject.toml
COPY services/contabilizacao/pyproject.toml services/contabilizacao/pyproject.toml
COPY libs/plataforma/pyproject.toml libs/plataforma/pyproject.toml
COPY relay/pyproject.toml relay/pyproject.toml
COPY legado-mock/pyproject.toml legado-mock/pyproject.toml

# Dependências de terceiros (sem instalar os membros do workspace ainda).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package contabilizacao --no-install-workspace

# Código do serviço, instalado como pacote (não editável) no venv.
COPY services/contabilizacao services/contabilizacao
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package contabilizacao --no-editable

# ---- runtime: só o venv, sem uv, sem pip, sem compilador, sem fontes ----
FROM python:3.13-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS runtime

# Sem instalador de pacotes no runtime: o pip da imagem base (e o que ele vendoriza: urllib3, certifi, msgpack,
# pkg_resources) não é usado pela aplicação, trazia CVEs HIGH com correção (painel 6, G5) e deixaria um `pip install`
# pronto para quem invadir o container. O ensurepip sai junto (guarda o wheel do pip). O python:3.13 já não traz
# setuptools nem wheel; o rm cobre a base se isso voltar.
RUN python -m pip uninstall --yes --no-input pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/lib/python3.13/site-packages/setuptools* \
        /usr/local/lib/python3.13/site-packages/wheel* /usr/local/bin/pip* /usr/local/bin/wheel

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Usuário não-root fixo (uid 10001), sem shell de login e sem home.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv

USER 10001:10001

# Sem HTTP: o loop toca /tmp/contabilizacao.heartbeat a cada iteração (long poll de 10 s);
# saudável se mexeu nos últimos 30 s.
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/contabilizacao.heartbeat')<30 else 1)"]

CMD ["python", "-m", "contabilizacao.adapters.inbound.poller"]
