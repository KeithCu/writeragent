# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PR CI hang-diagnostics inputs stay off by default and upload artifacts."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-ci.yml"


def _workflow() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _dispatch_inputs() -> str:
    return _workflow().split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0]


def test_pr_ci_debug_inputs_default_off() -> None:
    inputs = _dispatch_inputs()
    assert "ci_debug:" in inputs
    assert "pytest_serial:" in inputs
    assert inputs.count("default: false") >= 3


def test_pr_ci_debug_env_only_when_input_true() -> None:
    text = _workflow()
    assert "WRITERAGENT_CI_DEBUG:" in text
    assert "inputs.ci_debug == true && '1' || ''" in text
    # runner is not available in jobs.<job_id>.env (startup_failure 33452656845).
    job_env = text.split("    env:", 1)[1].split("    steps:", 1)[0]
    assert "runner" not in job_env
    assert "WRITERAGENT_CI_DEBUG_DIR" not in job_env
    assert 'export WRITERAGENT_CI_DEBUG_DIR="$RUNNER_TEMP/pytest-ci-debug"' in text


def test_pr_ci_debug_uploads_artifacts_and_process_dump() -> None:
    text = _workflow()
    assert "actions/upload-artifact@v4" in text
    assert "ci-debug-${{ matrix.os }}" in text
    assert "Dump leftover processes" in text
    assert "tasklist" in text
    assert "ps -ef" in text
    assert "harper-ls" in text
    assert "soffice" in text
    assert "if: ${{ always() && inputs.ci_debug == true }}" in text


def test_pr_ci_test_step_has_timeout_and_serial_escape() -> None:
    text = _workflow()
    test_step = text.split("Run tests (Pytest + UNO)", 1)[1].split(
        "Dump leftover processes", 1
    )[0]
    assert "timeout-minutes: 25" in test_step
    assert "PYTEST_WORKERS=0" in test_step
    assert "inputs.pytest_serial" in test_step


def test_pr_ci_job_timeout_grows_only_for_debug() -> None:
    text = _workflow()
    assert "inputs.ci_debug == true && 40 || 30" in text
