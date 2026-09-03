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


def test_rdb_core_invokes_bash_script_not_windows_ps1() -> None:
    """GHA 33777144412: make rdb-core used rebuild_librepy_rdb.ps1 (missing)."""
    makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("rdb-core:", 1)[1].split("build-core:", 1)[0]
    assert "rebuild_librepy_rdb.sh" in recipe
    assert "rebuild_librepy_rdb$(EXT)" not in recipe
    assert "rebuild_librepy_rdb.ps1" not in recipe


def test_windows_ci_installs_opengrep_and_forces_utf8() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    windows_block = text.split("Install LibreOffice and system tools (Windows)", 1)[1]
    windows_block = windows_block.split("Install uv", 1)[0]
    assert "install.ps1" in windows_block
    assert ".opengrep/cli/latest" in windows_block
    assert "PYTHONUTF8" in text
    assert 'unopkg list | grep -q' not in text
