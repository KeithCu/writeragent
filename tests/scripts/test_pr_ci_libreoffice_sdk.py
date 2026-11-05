"""Ubuntu PR CI must ship unoidl-write so ``make rdb-core`` can run.

``make build-core`` always rebuilds ``extension-core/XPythonFunction.rdb``.
That step calls ``unoidl-write`` from the LibreOffice SDK. On Ubuntu the
binary is in ``libreoffice-dev``, not the ``libreoffice`` metapackage.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-ci.yml"
_RELEASE_VERIFY = _REPO_ROOT / ".github" / "workflows" / "release-verify.yml"
_UBUNTU_LO_APT_ACTION = (
    _REPO_ROOT / ".github" / "actions" / "ubuntu-libreoffice-apt" / "action.yml"
)
_RDB_SCRIPT = _REPO_ROOT / "scripts" / "rebuild_librepy_rdb.sh"
_COMPOSITE_ACTION = "./.github/actions/ubuntu-libreoffice-apt"


def _ubuntu_lo_apt_pkgs_from_action() -> tuple[str, str]:
    """Return (pkgs= cache-key list, apt-get install package list)."""
    data = yaml.safe_load(_UBUNTU_LO_APT_ACTION.read_text(encoding="utf-8"))
    steps = data["runs"]["steps"]
    cache_run = steps[0]["run"]
    install_run = next(
        step["run"] for step in steps if step.get("name") == "apt-get install LibreOffice"
    )
    pkgs_assign = re.search(r'pkgs="([^"]*)"', cache_run)
    assert pkgs_assign is not None, "lo_cache_key step must set pkgs= for apt cache hash"
    apt_install = re.search(
        r"apt-get install -y (.+?); then",
        install_run,
        re.DOTALL,
    )
    assert apt_install is not None, "apt-get install step must list Ubuntu LO packages"
    return pkgs_assign.group(1), apt_install.group(1).strip()


def _linux_jobs_use_ubuntu_lo_composite(workflow_path: Path) -> None:
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") or {}
    for job_id, job in jobs.items():
        steps = job.get("steps") or []
        linux_lo_steps = [
            step
            for step in steps
            if step.get("uses") == _COMPOSITE_ACTION
            and step.get("if") == "runner.os == 'Linux'"
        ]
        assert linux_lo_steps, (
            f"{workflow_path.name} job {job_id!r} must install LibreOffice via "
            f"{_COMPOSITE_ACTION} when runner.os is Linux"
        )


def test_ubuntu_ci_installs_libreoffice_dev_for_rdb_core() -> None:
    cache_pkgs, install_pkgs = _ubuntu_lo_apt_pkgs_from_action()
    assert "libreoffice-dev" in cache_pkgs.split()
    assert "libreoffice-dev" in install_pkgs.split()
    assert cache_pkgs.split() == install_pkgs.split()
    _linux_jobs_use_ubuntu_lo_composite(_WORKFLOW)
    _linux_jobs_use_ubuntu_lo_composite(_RELEASE_VERIFY)


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
