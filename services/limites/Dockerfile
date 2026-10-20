# syntax=docker/dockerfile:1
# Build a partir da RAIZ do workspace (context = entrega/):
#   docker build -f services/limites/Dockerfile -t desafio-zup/limites:dev .

# Fonte do binário do uv (fixado na série 0.12).
FROM ghcr.io/astral-sh/uv:0.12 AS uv

# ---- build: resolve e instala só o necessário para o serviço limites num venv isolado ----
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
    uv sync --frozen --no-dev --package limites --no-install-workspace

# Código dos membros que o serviço usa, instalados como pacotes (não editáveis) no venv.
COPY libs/plataforma libs/plataforma
COPY services/limites services/limites
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package limites --no-editable

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
EXPOSE 8080

# Saúde via stdlib (a imagem slim não tem curl); urlopen levanta em status != 2xx.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/saude', timeout=2)"]

# Keep-alive acima dos 350 s de ociosidade do NLB: quem fecha conexão ociosa é o lado do gateway, nunca o serviço
# (com o default de 5 s o API Gateway reaproveitava conexão em fechamento → 500 "Connection is closed", visto na AWS).
# Sem access log por requisição: o API Gateway já registra cada uma (custo de CPU e de ingestão a 5.000 TPS).
CMD ["uvicorn", "limites.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-keep-alive", "400", "--no-access-log"]
