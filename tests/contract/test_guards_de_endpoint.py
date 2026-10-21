"""Todo cliente boto3 com endpoint explícito passa pela MESMA allowlist de endpoint local e pela mesma trava.

O guard existe em várias cópias (composition roots, conftests, bootstrap, mock do legado). Uma cópia que voltasse ao
parser de URL da biblioteca padrão aceitaria `http://exemplo.com\\@localhost/`, que o urllib3 do botocore disca para
`exemplo.com`; uma que esquecesse a trava deixaria `AWS_ENDPOINT_URL_<SERVIÇO>` desviar o cliente sem passar pelo
guard. Este teste acha os arquivos pelo código (rastreados ou não) e exige o mesmo guard em todos.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
ESTE = Path(__file__).resolve()
REGEX_CANONICA = (
    r'    r"https?://(?:127\.0\.0\.1|localhost|localstack)"',
    r'    r"(?::(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3}))?/?",',
    "    re.IGNORECASE | re.ASCII,",
)
# Cliente boto3 (client, resource ou Session) com endpoint explícito, em qualquer ordem de argumentos e com
# credencial literal, constante ou do ambiente. Testes unitários ficam de fora: usam Stubber ou boto3.client falso.
ENDPOINT_EXPLICITO = re.compile(r"\bendpoint_url\s*=(?!=)")
CRIA_CLIENTE = re.compile(r"\bboto3\.(?:client|resource|Session)\b")
CONHECIDOS = {
    "infra/local/bootstrap.py",
    "legado-mock/src/legado_mock/__main__.py",
    "relay/src/relay/local.py",
    "services/autorizacao/src/autorizacao/main.py",
    "services/autorizacao/tests/conftest.py",
    "services/contabilizacao/src/contabilizacao/main.py",
    "services/contabilizacao/tests/conftest.py",
    "services/limites/src/limites/main.py",
    "services/limites/tests/conftest.py",
}


def _arquivos_com_cliente_de_endpoint_explicito() -> set[str]:
    todos = subprocess.run(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "*.py"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    achados = set()
    for nome in todos:
        caminho = RAIZ / nome
        if "/tests/unit/" in nome or caminho.resolve() == ESTE or not caminho.exists():
            continue
        texto = caminho.read_text()
        if ENDPOINT_EXPLICITO.search(texto) and CRIA_CLIENTE.search(texto):
            achados.add(nome)
    return achados


def test_os_clientes_com_endpoint_explicito_sao_os_guards_conhecidos() -> None:
    achados = _arquivos_com_cliente_de_endpoint_explicito()
    assert achados == CONHECIDOS, (
        f"novos (ponha o guard canônico e acrescente a CONHECIDOS): {sorted(achados - CONHECIDOS)}; "
        f"sumidos (tire de CONHECIDOS): {sorted(CONHECIDOS - achados)}"
    )


@pytest.mark.parametrize("nome", sorted(CONHECIDOS))
def test_guard_canonico(nome: str) -> None:
    texto = (RAIZ / nome).read_text()
    assert "\n".join(REGEX_CANONICA) in texto, "regex diferente da canônica"
    assert not re.search(r"\burlparse\s*\(|import urlparse", texto), (
        "parser de URL no lugar da allowlist"
    )
    assert re.search(r"\bENDPOINT_[A-Z]+\.fullmatch\(", texto), "a allowlist precisa de fullmatch"
    assert not re.search(r"\bENDPOINT_[A-Z]+\.(?:match|search)\(", texto), (
        "match/search aceita sufixo"
    )
    assert "ignore_configured_endpoint_urls=True" in texto, "sem a trava de endpoint por variável"
