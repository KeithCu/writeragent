"""Ubuntu PR CI must ship unoidl-write so ``make rdb-core`` can run.

``make build-core`` always rebuilds ``extension-core/XPythonFunction.rdb``.
That step calls ``unoidl-write`` from the LibreOffice SDK. On Ubuntu the
binary is in ``libreoffice-dev``, not the ``libreoffice`` metapackage.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-ci.yml"
_RDB_SCRIPT = _REPO_ROOT / "scripts" / "rebuild_librepy_rdb.sh"


def test_ubuntu_ci_installs_libreoffice_dev_for_rdb_core() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    ubuntu_block = text.split("Install LibreOffice and system tools (Ubuntu)", 1)[1]
    ubuntu_block = ubuntu_block.split("Install LibreOffice and system tools (macOS)", 1)[0]
    assert "libreoffice-dev" in ubuntu_block


def test_rdb_script_names_ubuntu_sdk_package() -> None:
    text = _RDB_SCRIPT.read_text(encoding="utf-8")
    assert "libreoffice-dev" in text


def test_windows_ci_installs_opengrep_and_forces_utf8() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    windows_block = text.split("Install LibreOffice and system tools (Windows)", 1)[1]
    windows_block = windows_block.split("Install uv", 1)[0]
    assert "install.ps1" in windows_block
    assert ".opengrep/cli/latest" in windows_block
    assert "PYTHONUTF8" in text
    assert 'unopkg list | grep -q' not in text
