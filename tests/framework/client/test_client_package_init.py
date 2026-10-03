"""plugin.framework.client package init must not load the LLM stack."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLIENT_INIT = _REPO_ROOT / "plugin" / "framework" / "client" / "__init__.py"

_FORBIDDEN_TOP_LEVEL = frozenset(
    {
        "llm_client",
        ".llm_client",
        "embedding_client",
        ".embedding_client",
        "embeddings_service",
        ".embeddings_service",
        "stream_normalizer",
        ".stream_normalizer",
        "plugin.scripting.client",
    }
)


def _top_level_import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_client_init_source_does_not_import_llm_stack():
    modules = _top_level_import_modules(_CLIENT_INIT)
    hit = [m for m in modules if m in _FORBIDDEN_TOP_LEVEL]
    assert not hit, "client/__init__.py top-level imports LLM stack: %s" % hit


def test_import_requests_does_not_load_llm_client():
    code = (
        "import sys\n"
        "from plugin.framework.client.requests import sync_request\n"
        "assert sync_request is not None\n"
        "assert 'plugin.framework.client.llm_client' not in sys.modules\n"
        "assert 'plugin.embeddings.embedding_client' not in sys.modules\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_extension_update_check_does_not_load_llm_client():
    code = (
        "import sys\n"
        "from plugin.chatbot.extension_update_check import schedule_extension_update_check_once\n"
        "from plugin.framework.client.requests import sync_request\n"
        "assert schedule_extension_update_check_once is not None\n"
        "assert sync_request is not None\n"
        "assert 'plugin.framework.client.llm_client' not in sys.modules\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
