# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the agent-callable report_bug tool (no LibreOffice required)."""
import json
import os
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.doc.document_research_tools import ReportBug, ListOpenDocuments, _append_feedback_log, _FEEDBACK_LOG_FILENAME


def test_doc_agnostic_tools_do_not_require_a_document():
    # Live finding (2026-06-28): the MCP no-document gate blocks tools whose `requires_document`
    # is the default True. report_bug and list_open_documents must work with no document open.
    assert ReportBug.requires_document is False
    assert ListOpenDocuments.requires_document is False


def test_append_feedback_log_writes_jsonl(tmp_path):
    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)):
        path = _append_feedback_log("ux", "confusing result", "expected X got Y", ts="2026-06-27T19:00:00")
    assert path and path.endswith(_FEEDBACK_LOG_FILENAME)
    rec = json.loads(open(path, encoding="utf-8").read().strip())
    assert rec == {"ts": "2026-06-27T19:00:00", "category": "ux", "summary": "confusing result", "details": "expected X got Y"}


def test_append_feedback_log_fails_safe(tmp_path):
    # If the config dir can't be resolved, return None (never raise).
    with patch("plugin.framework.config.user_config_dir", side_effect=RuntimeError("no profile")):
        assert _append_feedback_log("bug", "x", "y") is None


def test_append_feedback_log_returns_none_when_no_config_dir():
    with patch("plugin.framework.config.user_config_dir", return_value=None):
        assert _append_feedback_log("bug", "x", "y") is None


def test_report_bug_logs_and_returns_url(tmp_path):
    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.bug_report.build_github_issue_url",
               return_value="https://github.com/KeithCu/writeragent/issues/new?title=x"):
        res = ReportBug().execute(MagicMock(), summary="apply_document_content lied", details="said ok but nothing changed", category="bug")
    assert res["status"] == "ok"
    assert res["github_issue_url"].startswith("https://github.com/KeithCu/writeragent/issues/new")
    assert res["category"] == "bug"
    assert res["logged_to"] and os.path.exists(res["logged_to"])
    rec = json.loads(open(res["logged_to"], encoding="utf-8").read().strip())
    assert rec["summary"] == "apply_document_content lied"


def test_report_bug_requires_summary():
    res = ReportBug().execute(MagicMock(), summary="   ")
    assert res["status"] == "error"


def test_report_bug_invalid_category_defaults_to_bug(tmp_path):
    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.bug_report.build_github_issue_url", return_value="u"):
        res = ReportBug().execute(MagicMock(), summary="hi", category="nonsense")
    assert res["category"] == "bug"


def test_report_bug_does_not_autosubmit(tmp_path):
    # Safety: the tool must NOT publish anything by itself — only return a URL for the user.
    with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.bug_report.build_github_issue_url", return_value="u"):
        res = ReportBug().execute(MagicMock(), summary="x")
    assert "auto-submitted" in res["message"].lower() or "auto-filing" in res["message"].lower()
