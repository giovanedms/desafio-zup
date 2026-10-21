"""Todo cliente boto3 com endpoint explícito passa pela MESMA allowlist de endpoint local e pela mesma trava.

O guard existe em várias cópias (composition roots, conftests, bootstrap, mock do legado). Uma cópia que voltasse ao
parser de URL da biblioteca padrão aceitaria `http://exemplo.com\\@localhost/`, que o urllib3 do botocore disca para
`exemplo.com`; uma que esquecesse a trava deixaria `AWS_ENDPOINT_URL_<SERVIÇO>` desviar o cliente sem passar pelo
guard. Este teste acha os arquivos pelo código e exige o mesmo guard em todos: num checkout, pelo git (rastreados ou
não); sem git (o ZIP da entrega é um `git archive`, sem `.git`), varrendo o disco.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
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
# Varredura sem git: o que não é código do repositório (ambientes virtuais, caches, build, dependências, Terraform).
# Também fica de fora todo diretório que é outro repositório/worktree (tem `.git`) ou ambiente virtual (`pyvenv.cfg`).
IGNORADOS_SEM_GIT = frozenset(
    {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
        ".import_linter_cache", ".mutmut-cache", ".tox", ".nox", "htmlcov", "build", "dist", "target", ".gradle",
        "node_modules", ".terraform", ".terraform-offline", "localstack-data",
    }
)  # fmt: skip


def _python_pelo_git(*, so_rastreados: bool = False) -> list[str] | None:
    """Os `.py` que o git vê (rastreados e, por padrão, não rastreados fora do .gitignore); None se RAIZ não é a raiz
    de um checkout: git ausente, ZIP extraído fora de repositório ou dentro de um repositório alheio."""
    try:
        topo = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if not topo or Path(topo).resolve() != RAIZ:
        return None
    opcoes = ["-c"] if so_rastreados else ["-c", "-o", "--exclude-standard"]
    # -z: sem ele o git põe entre aspas e escapa nome com acento, e o arquivo seria pulado como inexistente
    saida = subprocess.run(
        ["git", "ls-files", "-z", *opcoes, "*.py"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [nome for nome in saida.split("\0") if nome]


def _python_pelo_disco() -> list[str]:
    nomes = []
    for pasta, subpastas, arquivos in os.walk(RAIZ):
        subpastas[:] = [
            s
            for s in subpastas
            if s not in IGNORADOS_SEM_GIT
            and not (Path(pasta, s, ".git").exists() or Path(pasta, s, "pyvenv.cfg").exists())
        ]
        nomes += [
            Path(pasta, a).relative_to(RAIZ).as_posix() for a in arquivos if a.endswith(".py")
        ]
    return nomes


def _arquivos_com_cliente_de_endpoint_explicito(nomes: Iterable[str]) -> set[str]:
    achados = set()
    for nome in nomes:
        caminho = RAIZ / nome
        if "/tests/unit/" in nome or caminho.resolve() == ESTE or not caminho.exists():
            continue
        texto = caminho.read_text(encoding="utf-8")
        if ENDPOINT_EXPLICITO.search(texto) and CRIA_CLIENTE.search(texto):
            achados.add(nome)
    return achados


def test_os_clientes_com_endpoint_explicito_sao_os_guards_conhecidos() -> None:
    pelo_git = _python_pelo_git()
    achados = _arquivos_com_cliente_de_endpoint_explicito(
        _python_pelo_disco() if pelo_git is None else pelo_git
    )
    assert achados == CONHECIDOS, (
        f"[varredura {'do disco' if pelo_git is None else 'do git'}] "
        f"novos (ponha o guard canônico e acrescente a CONHECIDOS): {sorted(achados - CONHECIDOS)}; "
        f"sumidos (tire de CONHECIDOS): {sorted(CONHECIDOS - achados)}"
    )


def test_a_varredura_sem_git_ve_todo_python_rastreado() -> None:
    """O ZIP é o conjunto rastreado (`git archive HEAD`): se a varredura do disco não esconde nenhum `.py` rastreado,
    no ZIP ela vê todo `.py` da entrega, inclusive um guard novo que entrou no commit."""
    rastreados = _python_pelo_git(so_rastreados=True)
    if rastreados is None:
        pytest.skip(
            "fora de um checkout git (ZIP da entrega): a varredura do disco é a única fonte"
        )
    escondidos = {n for n in rastreados if (RAIZ / n).exists()} - set(_python_pelo_disco())
    assert not escondidos, f"a varredura sem git (ZIP) não veria: {sorted(escondidos)}"


@pytest.mark.parametrize("nome", sorted(CONHECIDOS))
def test_guard_canonico(nome: str) -> None:
    texto = (RAIZ / nome).read_text(encoding="utf-8")
    assert "\n".join(REGEX_CANONICA) in texto, "regex diferente da canônica"
    assert not re.search(r"\burlparse\s*\(|import urlparse", texto), (
        "parser de URL no lugar da allowlist"
    )
    assert re.search(r"\bENDPOINT_[A-Z]+\.fullmatch\(", texto), "a allowlist precisa de fullmatch"
    assert not re.search(r"\bENDPOINT_[A-Z]+\.(?:match|search)\(", texto), (
        "match/search aceita sufixo"
    )
    assert "ignore_configured_endpoint_urls=True" in texto, "sem a trava de endpoint por variável"
