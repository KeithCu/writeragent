# Tests for .semgrep/uno_thread_safety.yml (Layer C UNO thread-safety lint via Opengrep).

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".semgrep" / "uno_thread_safety.yml"
VIOLATIONS = ROOT / ".semgrep" / "uno_thread_safety.violations.py"
OK_FIXTURE = ROOT / ".semgrep" / "uno_thread_safety.ok.py"
OPENGREP = Path(os.environ.get("OPENGREP", "")) if os.environ.get("OPENGREP") else None
if OPENGREP is None or not OPENGREP.is_file():
    _local = ROOT / "bin" / "opengrep"
    _path = subprocess.run(["bash", "-lc", "command -v opengrep"], capture_output=True, text=True, check=False).stdout.strip()
    OPENGREP = Path(_path) if _path else _local


def _run_opengrep(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = [str(OPENGREP), "scan", *args]
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _scan_json(*targets: str) -> list[dict]:
    result = _run_opengrep("--json", "--severity", "ERROR", "--taint-intrafile", "-c", str(CONFIG), *targets)
    assert result.returncode in (0, 1), result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload.get("results", [])


def test_opengrep_available():
    assert OPENGREP.is_file(), f"opengrep not found at {OPENGREP} — run: make opengrep-install"


def test_opengrep_fixture_violations():
    findings = _scan_json(str(VIOLATIONS))
    rules = {item["check_id"].split(".")[-1] for item in findings}
    assert "uno-off-main-thread" in rules
    assert "raw-uno-thread-ban" in rules
    cross_fn = [f for f in findings if f["check_id"].endswith("uno-off-main-thread") and f["start"]["line"] == 32]
    assert cross_fn, "expected nested cross-function @background worker finding (Opengrep --taint-intrafile)"


def test_opengrep_fixture_ok():
    findings = _scan_json(str(OK_FIXTURE))
    assert not any(f["check_id"].endswith("uno-off-main-thread") for f in findings)


def test_opengrep_uno_thread_lint_clean_on_plugin():
    """Layer C rules must pass on production plugin/ (contrib excluded via .semgrepignore)."""
    result = _run_opengrep("--error", "--severity", "ERROR", "--taint-intrafile", "-c", str(CONFIG), "plugin")
    assert result.returncode == 0, result.stdout + result.stderr
