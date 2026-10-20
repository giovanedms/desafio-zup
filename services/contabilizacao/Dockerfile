# syntax=docker/dockerfile:1
# Build a partir da RAIZ do workspace (context = entrega/):
#   docker build -f services/contabilizacao/Dockerfile -t desafio-zup/contabilizacao:dev .

# Fonte do binário do uv (fixado na série 0.12).
FROM ghcr.io/astral-sh/uv:0.12 AS uv

# ---- build: resolve e instala só o necessário para o serviço contabilizacao num venv isolado ----
FROM python:3.13-slim AS build
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

# ---- runtime: só o venv, sem uv, sem compilador, sem fontes ----
FROM python:3.13-slim AS runtime

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
