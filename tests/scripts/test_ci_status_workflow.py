# WriterAgent tests — CI Status Pages workflow wiring
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-status.yml"


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_ci_status_workflow_triggers() -> None:
    text = _text()
    assert "workflow_dispatch:" in text
    assert "workflow_run:" in text
    assert "PR CI" in text
    assert "CrossHair Verification (Deep / On-Demand)" in text
    assert "cron:" in text
    assert "generate_ci_status.py" in text


def test_ci_status_workflow_pages_permissions_and_deploy() -> None:
    text = _text()
    assert "pages: write" in text
    assert "id-token: write" in text
    assert "actions: read" in text
    assert "environment:" in text
    assert "github-pages" in text
    assert "actions/upload-pages-artifact" in text
    assert "actions/deploy-pages" in text
    assert "https://keithcu.com" not in text
    assert "secrets.GITHUB_TOKEN" in text
    assert "PAT" not in text
