# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ApplyDocumentContent dry-run coverage. No LibreOffice."""
from unittest.mock import MagicMock, patch

import pytest

def _edit_ctx():
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    # dry_run sweeps shapes/comments; bare MagicMock enumerations never terminate.
    # Outline-hyperlink preview also walks portions; that stop is `_enum_has_more`
    # (UNO True only), not this stub.
    ctx.doc.getDrawPage.return_value = []
    ctx.doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    return ctx

# ---- D1: dry_run ------------------------------------------------------------

@pytest.mark.timeout(5)
def test_dry_run_reports_matches_without_mutating():
    from plugin.writer.content import ApplyDocumentContent

    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "clause 3.2 text"
    r2.getString.return_value = "clause 3.2 again"
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(ctx, content=["x"], target="search", old_content="clause 3.2", dry_run=True)
    assert res["status"] == "ok" and res["dry_run"] is True and res["count"] == 2
    assert res["matches"][0]["location"] == "body"
    # No session/undo context opened for a dry run.
    ctx.doc.getUndoManager.assert_not_called()


def test_dry_run_requires_search_target():
    from plugin.writer.content import ApplyDocumentContent
    res = ApplyDocumentContent().execute(_edit_ctx(), content=["x"], target="end", dry_run=True)
    assert res["status"] == "error" and "search" in res["message"]


@pytest.mark.timeout(5)
def test_dry_run_honors_regex_via_the_same_matcher_as_the_edit():
    """A preview that uses a different matcher than the commit is worse than none: with
    regex=true, dry_run must route through find_ranges_regex_case with the RAW pattern."""
    from plugin.writer.content import ApplyDocumentContent

    r = MagicMock()
    r.getString.return_value = "bravo charlie"
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_ranges_regex_case", return_value=[r]) as frc, \
         patch("plugin.writer.search.find_all_ranges") as far, \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content=r"brav. charl.e", dry_run=True, regex=True)
    assert res["status"] == "ok" and res["count"] == 1
    far.assert_not_called()
    args = frc.call_args[0]
    assert args[1] == r"brav. charl.e" and args[2] is True  # raw pattern, regex on


def test_dry_run_invalid_regex():
    from plugin.writer.content import ApplyDocumentContent

    ctx = _edit_ctx()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content="([a-", dry_run=True, regex=True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"
