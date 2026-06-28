# Tests for .semgrep/uno_thread_safety.yml (Layer C UNO thread-safety lint).

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
SEMgrep = ROOT / ".venv" / "bin" / "semgrep"


def _run_semgrep(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SEMGREP_SEND_METRICS": "off"}
    cmd = [str(SEMgrep), *args] if SEMgrep.is_file() else [sys.executable, "-m", "semgrep", *args]
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _scan_json(*targets: str) -> list[dict]:
    result = _run_semgrep("--json", "--config", str(CONFIG), *targets)
    assert result.returncode in (0, 1), result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload.get("results", [])


def test_semgrep_fixture_violations():
    findings = _scan_json(str(VIOLATIONS))
    rules = {item["check_id"].split(".")[-1] for item in findings}
    assert "uno-off-main-thread" in rules
    assert "raw-uno-thread-ban" in rules
    assert len([f for f in findings if f["check_id"].endswith("uno-off-main-thread")]) >= 3


def test_semgrep_fixture_ok():
    findings = _scan_json(str(OK_FIXTURE))
    assert not any(f["check_id"].endswith("uno-off-main-thread") for f in findings)


def test_semgrep_uno_thread_lint_clean_on_plugin():
    """Layer C rules must pass on production plugin/ (contrib excluded via .semgrepignore)."""
    result = _run_semgrep("--error", "--config", str(CONFIG), "plugin")
    assert result.returncode == 0, result.stdout + result.stderr
