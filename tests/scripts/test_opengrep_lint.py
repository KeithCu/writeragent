# Tests for Opengrep Layer C (UNO thread safety + vendored security rules).

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMGREP_DIR = ROOT / "tests" / "semgrep"
UNO_CONFIG = SEMGREP_DIR / "uno_thread_safety.yml"
SECURITY_CONFIG = SEMGREP_DIR / "writeragent_security.yml"
THIRD_PARTY = SEMGREP_DIR / "third_party"
SOURCES_JSON = THIRD_PARTY / "SOURCES.json"
UNO_VIOLATIONS = SEMGREP_DIR / "uno_thread_safety.violations.py"
UNO_OK_FIXTURE = SEMGREP_DIR / "uno_thread_safety.ok.py"
SECURITY_VIOLATIONS = SEMGREP_DIR / "security_rules.violations.py"
OPENGREP_EXCLUDE_ARGS = ["--exclude=plugin/contrib", "--exclude=plugin/lib"]

OPENGREP_CONFIGS = [
    UNO_CONFIG,
    SECURITY_CONFIG,
    THIRD_PARTY / "semgrep-rules",
    THIRD_PARTY / "trailofbits",
]

OPENGREP = Path(os.environ.get("OPENGREP", "")) if os.environ.get("OPENGREP") else None
if OPENGREP is None or not OPENGREP.is_file():
    _local = ROOT / "bin" / "opengrep"
    _path = subprocess.run(["bash", "-lc", "command -v opengrep"], capture_output=True, text=True, check=False).stdout.strip()
    OPENGREP = Path(_path) if _path else _local


def _config_args() -> list[str]:
    args: list[str] = []
    for cfg in OPENGREP_CONFIGS:
        args.extend(["-c", str(cfg)])
    return args


def _run_opengrep(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("SEMGREP_SEND_METRICS", "off")
    cmd = [str(OPENGREP), "scan", *args]
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _scan_json(*extra: str, configs: list[Path] | None = None) -> list[dict]:
    cfg_args: list[str] = []
    for cfg in configs or OPENGREP_CONFIGS:
        cfg_args.extend(["-c", str(cfg)])
    result = _run_opengrep(
        "--json",
        "--severity",
        "ERROR",
        "--taint-intrafile",
        *OPENGREP_EXCLUDE_ARGS,
        *cfg_args,
        *extra,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload.get("results", [])


def test_opengrep_available():
    assert OPENGREP.is_file(), f"opengrep not found at {OPENGREP} — run: make opengrep-install"


def test_third_party_rules_present():
    assert SOURCES_JSON.is_file(), "missing tests/semgrep/third_party/SOURCES.json — run: make opengrep-rules-sync"
    payload = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    assert payload.get("sources"), "SOURCES.json must list pinned upstream sources"
    assert (THIRD_PARTY / "semgrep-rules" / "python" / "lang" / "security" / "use-defused-xml.yaml").is_file()
    assert (THIRD_PARTY / "trailofbits" / "python" / "tarfile-extractall-traversal.yaml").is_file()


def test_opengrep_uno_fixture_violations():
    findings = _scan_json(str(UNO_VIOLATIONS), configs=[UNO_CONFIG])
    rules = {item["check_id"].split(".")[-1] for item in findings}
    assert "uno-off-main-thread" in rules
    assert "raw-uno-thread-ban" in rules
    cross_fn = [f for f in findings if f["check_id"].endswith("uno-off-main-thread") and f["start"]["line"] == 32]
    assert cross_fn, "expected nested cross-function @background worker finding (Opengrep --taint-intrafile)"


def test_opengrep_uno_fixture_ok():
    findings = _scan_json(str(UNO_OK_FIXTURE), configs=[UNO_CONFIG])
    assert not any(f["check_id"].endswith("uno-off-main-thread") for f in findings)


def test_opengrep_security_fixture_violations():
    findings = _scan_json(str(SECURITY_VIOLATIONS))
    rule_suffixes = {item["check_id"].split(".")[-1] for item in findings}
    assert "writeragent-no-tempfile-mktemp" in rule_suffixes
    assert "subprocess-shell-true" in rule_suffixes


def test_opengrep_lint_clean_on_plugin():
    """Combined UNO + security rules must pass on production plugin/ (contrib excluded via OPENGREP_EXCLUDE_ARGS)."""
    result = _run_opengrep(
        "--error",
        "--severity",
        "ERROR",
        "--taint-intrafile",
        *OPENGREP_EXCLUDE_ARGS,
        *_config_args(),
        "plugin",
    )
    assert result.returncode == 0, result.stdout + result.stderr
