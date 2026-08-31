"""PR CI mock-sidebar option stays off by default and skips the other jobs when on."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-ci.yml"


def _workflow() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_pr_ci_exposes_test_mock_sidebar_checkbox_default_off() -> None:
    text = _workflow()
    assert "test_mock_sidebar:" in text
    inputs = text.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0]
    assert "type: boolean" in inputs
    assert "default: false" in inputs


def test_pr_ci_mock_sidebar_uses_existing_make_target() -> None:
    text = _workflow()
    assert "make test-mock-sidebar" in text
    assert "make mock-llm-sidebar" not in text


def test_pr_ci_mock_sidebar_skips_other_suites() -> None:
    text = _workflow()
    assert "if: ${{ inputs.test_mock_sidebar != true }}" in text
    assert "if: ${{ inputs.test_mock_sidebar == true }}" in text
    typecheck = text.split("Run typecheck", 1)[1]
    typecheck = typecheck.split("Build & install WriterAgent", 1)[0]
    assert "inputs.test_mock_sidebar != true" in typecheck
    pytest_block = text.split("Run tests (Pytest + UNO)", 1)[1]
    pytest_block = pytest_block.split("Verify Core", 1)[0]
    assert "inputs.test_mock_sidebar != true" in pytest_block
    packaging = text.split("Verify Core & Grammar", 1)[1]
    packaging = packaging.split("Run mock LLM sidebar", 1)[0]
    assert "inputs.test_mock_sidebar != true" in packaging


def test_pr_ci_linux_mock_sidebar_provides_display() -> None:
    text = _workflow()
    assert "xvfb-run" in text
    assert "dbus-run-session" in text
