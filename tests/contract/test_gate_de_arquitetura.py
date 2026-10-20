"""O gate de arquitetura (import-linter, ADR-0019) reprova de verdade um import de AWS SDK no núcleo.

Roda o `lint-imports` com o `.importlinter` REAL do repositório contra um pacote-fixture em `tmp_path` que
se passa pelo pacote alvo (mesmo nome e mesmas camadas). O fixture vem antes no PYTHONPATH, então sombreia
o pacote instalado só no subprocesso. `--no-cache`: nada é escrito fora do `tmp_path`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]

# pacote → (módulos das camadas, módulo do núcleo, nome do contrato `forbidden` que deve ficar BROKEN)
CASOS = {
    "autorizacao": (
        ("adapters", "application", "domain"),
        "domain",
        "autorizacao: domain e application sem framework nem AWS SDK",
    ),
    "relay": (
        (
            "local",
            "lambda_handler",
            "sweeper",
            "cdc_limites",
            "eventbridge",
            "outbox_dynamodb",
            "nucleo",
        ),
        "nucleo",
        "relay: nucleo sem AWS SDK",
    ),
}


def _fixture(raiz: Path, pacote: str, camadas: tuple[str, ...]) -> Path:
    (raiz / pacote).mkdir(parents=True)
    (raiz / pacote / "__init__.py").write_text("", encoding="utf-8")
    for camada in camadas:
        (raiz / pacote / f"{camada}.py").write_text("", encoding="utf-8")
    return raiz / pacote


def _lint_imports(pythonpath: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--config",
            str(RAIZ / ".importlinter"),
            "--no-cache",
        ],
        cwd=pythonpath,
        env={**os.environ, "PYTHONPATH": str(pythonpath)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("pacote", sorted(CASOS))
def test_import_de_boto3_no_nucleo_deixa_o_contrato_broken(tmp_path: Path, pacote: str) -> None:
    camadas, nucleo, contrato = CASOS[pacote]
    diretorio = _fixture(tmp_path, pacote, camadas)

    limpo = _lint_imports(tmp_path)  # controle: o fixture sozinho cumpre todos os contratos
    assert limpo.returncode == 0, limpo.stdout + limpo.stderr

    (diretorio / f"{nucleo}.py").write_text("import boto3  # noqa: F401\n", encoding="utf-8")
    sujo = _lint_imports(tmp_path)

    assert sujo.returncode == 1, sujo.stdout + sujo.stderr
    assert f"{contrato} BROKEN" in sujo.stdout
    # a linha do import só existe no fixture: prova que foi ele o analisado, não o pacote instalado
    assert f"{pacote}.{nucleo} -> boto3" in sujo.stdout
    assert re.search(r"Contracts: \d+ kept, 1 broken\.", sujo.stdout)  # só este contrato quebrou
