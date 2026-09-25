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


# ---- 5) position='before'/'after' contract ------------------------------------

def test_position_param_validation():
    from plugin.writer.content import ApplyDocumentContent

    tool = ApplyDocumentContent()
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    res = tool.execute(ctx, content=["<p>x</p>"], target="search", old_content="y", position="sideways")
    assert res["status"] == "error" and "position" in res["message"]
    res = tool.execute(ctx, content=["<p>x</p>"], target="search", old_content="y",
                       position="after", all_matches=True)
    assert res["status"] == "error" and "all_matches" in res["message"]
    # Silently ignoring position on an insert target would teach a parameter that "works" by
    # accident — it must be rejected.
    res = tool.execute(ctx, content=["<p>x</p>"], target="end", position="after")
    assert res["status"] == "error" and "target='search'" in res["message"]


def test_position_after_inserts_at_match_edge_without_replacing():
    """The Q8 happy path: collapsed cursor at the found range's edge, HTML import WITHOUT
    styles, result carries inserted=true/position and NO replaced_count."""
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    # Not inside a table cell:
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False

    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>novo</p>"], target="search", old_content="clausula", position="after")

    assert res["status"] == "ok" and res["inserted"] is True and res["position"] == "after"
    assert "replaced_count" not in res
    ins.assert_called_once()
    assert ins.call_args.kwargs.get("apply_styles") is False
    # The match edge used must be getEnd() for 'after'.
    found.getStart.assert_called()


def test_position_before_inserts_at_match_start():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>prefix</p>"], target="search", old_content="clause", position="before")
    assert res["status"] == "ok" and res["inserted"] is True and res["position"] == "before"
    ins.assert_called_once()
    found.getStart.assert_called()


def test_position_after_rejects_math_and_table_cells():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False

    found = MagicMock()
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=True):
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>\\(x^2\\)</p>"], target="search", old_content="c", position="after")
    assert res["status"] == "error" and "math" in res["message"]

    cell_cursor = MagicMock()
    cell_cursor.getPropertyValue.return_value = MagicMock()  # TextTable set -> inside a cell
    found.getText.return_value.createTextCursorByRange.return_value = cell_cursor
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>x</p>"], target="search", old_content="c", position="after")
    assert res["status"] == "error" and "table cell" in res["message"]


def test_position_in_schema():
    from plugin.writer.content import ApplyDocumentContent

    props = ApplyDocumentContent.parameters["properties"]
    assert props["position"]["enum"] == ["replace", "before", "after"]



def test_truncated_flag_on_get_document_content():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 800
    with patch.object(format_support, "document_to_content",
                      return_value="<p>short</p>\n\n[... truncated ...]"):
        res = GetDocumentContent().execute(ctx, max_chars=10)
    assert res.get("truncated") is True
    with patch.object(format_support, "document_to_content", return_value="<p>all</p>"):
        res = GetDocumentContent().execute(ctx, max_chars=10)
    assert "truncated" not in res


def test_get_document_content_surfaces_portion_walk_warning():
    """Range/selection paint that hits _COPY_PORTION_LIMIT must not look complete."""
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import GetDocumentContent

    ctx = MagicMock()
    ctx.services.document.get_document_length.return_value = 800

    def _fake_export(*_args, **kwargs):
        dest = kwargs.get("walk_warnings")
        if dest is not None:
            dest.append(
                "Walk stopped after 2 text portions (cap 2). Later content was not read, "
                "so formatting may be incomplete."
            )
        return "<p>partial</p>"

    with patch.object(format_support, "document_to_content", side_effect=_fake_export):
        res = GetDocumentContent().execute(ctx, scope="range", start=0, end=10)

    assert res["status"] == "ok"
    assert "incomplete" in res["warning"]


def test_get_document_content_surfaces_tracked_changes():
    from plugin.writer.content import GetDocumentContent

    doc = MagicMock()
    doc.getPropertyValue.return_value = True
    doc.getRedlines.return_value.getCount.return_value = 1
    text = MagicMock()
    doc.getText.return_value = text
    ctx = MagicMock()
    ctx.doc = doc
    ctx.ctx = MagicMock()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.content.collect_tracked_changes", return_value=[{"type": "Insert", "text": "x"}]), \
         patch("plugin.writer.format.document_to_content", return_value="body"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert res["tracked_changes"] == [{"type": "Insert", "text": "x"}]
    assert "tracked_changes_note" in res
