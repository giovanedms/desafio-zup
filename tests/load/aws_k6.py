"""Roda o k6 contra o API Gateway da janela AWS sem expor segredos (lidos do terraform output, passados por env).

Uso: AWS_PROFILE=desafio-zup uv run python tests/load/aws_k6.py RPS=200 DURACAO=60s
"""

from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, "tests/e2e")
import aws

s = aws.saidas()
env = aws.ambiente_aws()
env.update(
    {
        "CENARIO": "vazao",
        "BASE_URL": s["invoke_url"],
        "CLIENT_ID": "canal-carga",
        "API_KEY": s["api_keys"]["canal-carga"],
        "TOKEN_URL": s["token_endpoint"],
        "OAUTH_CLIENT_ID": s["cognito_client_ids"]["canal-carga"],
        "OAUTH_CLIENT_SECRET": s["cognito_client_secrets"]["canal-carga"],
    }
)
for arg in sys.argv[1:]:
    chave, _, valor = arg.partition("=")
    env[chave] = valor
k6 = ["k6", "run", "--quiet", "tests/load/autorizacao.js"]
sys.exit(subprocess.run(k6, env=env, check=False).returncode)
