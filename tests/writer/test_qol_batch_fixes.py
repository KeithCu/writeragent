# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 8-point QoL/bug batch (2026-07-02): reads must not save, is_active must be proxy-safe,
misses must be errors (regex, delete_comment), position='before'/'after' inserts, document echo,
undo/redo exposure, and recoverable error messages. No LibreOffice required."""
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()


# ---- 1) reads never doc.store() ----------------------------------------------

def test_ensure_heading_bookmarks_never_stores():
    import inspect

    from plugin.writer.specialized import bookmarks as bm

    # Strip comments: the fix intentionally documents WHY there is no doc.store() call.
    code_lines = [line.split("#")[0] for line in inspect.getsource(bm).splitlines()]
    assert not any("doc.store()" in line for line in code_lines), \
        "a READ/maintenance path must never persist the user's unsaved work"


# (is_active proxy-safe + document echo + multi-doc guidance tests live in the MCP experience PR,
# tests/mcp/test_mcp_qol_extras.py — they exercise document_research/mcp_protocol/agent_manual.)

# ---- 3) invalid regex is an error, not a clean miss ---------------------------

def _search_zero_hits(pattern, use_regex):
    """Run SearchInDocument against a doc that finds nothing."""
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = None
    doc.getDrawPage.return_value = []
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock()
    ctx.doc = doc
    return SearchInDocument().execute(ctx, pattern=pattern, regex=use_regex)


def test_invalid_regex_zero_hits_is_error():
    res = _search_zero_hits("([a-", True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"
    assert "regex=false" in res["message"]


def test_valid_regex_zero_hits_stays_ok():
    res = _search_zero_hits("nunca_existe_\\d+", True)
    assert res["status"] == "ok" and res["count"] == 0


def test_literal_zero_hits_stays_ok():
    res = _search_zero_hits("([a-", False)  # literal search for weird chars is legitimate
    assert res["status"] == "ok" and res["count"] == 0


# ---- 4) delete_comment miss is an error ---------------------------------------

def test_delete_comment_not_found_is_error():
    from plugin.writer.specialized.comments import DeleteComment

    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock()
    ctx.doc = doc
    res = DeleteComment().execute(ctx, comment_name="nope")
    assert res["status"] == "error" and res["code"] == "COMMENT_NOT_FOUND"
    assert res["deleted"] == 0
    assert "list_comments" in res["message"]


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

    with patch("plugin.writer.content._find_first_range", return_value=found), \
         patch("plugin.writer.content._collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "_insert_mixed_or_plain_html") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>novo</p>"], target="search", old_content="clausula", position="after")

    assert res["status"] == "ok" and res["inserted"] is True and res["position"] == "after"
    assert "replaced_count" not in res
    ins.assert_called_once()
    assert ins.call_args.kwargs.get("apply_styles") is False
    # The match edge used must be getEnd() for 'after'.
    found.getEnd.assert_called()


def test_position_after_rejects_math_and_table_cells():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False

    found = MagicMock()
    with patch("plugin.writer.content._find_first_range", return_value=found), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=True):
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>\\(x^2\\)</p>"], target="search", old_content="c", position="after")
    assert res["status"] == "error" and "math" in res["message"]

    cell_cursor = MagicMock()
    cell_cursor.getPropertyValue.return_value = MagicMock()  # TextTable set -> inside a cell
    found.getText.return_value.createTextCursorByRange.return_value = cell_cursor
    with patch("plugin.writer.content._find_first_range", return_value=found), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>x</p>"], target="search", old_content="c", position="after")
    assert res["status"] == "error" and "table cell" in res["message"]


def test_position_in_schema():
    from plugin.writer.content import ApplyDocumentContent

    props = ApplyDocumentContent.parameters["properties"]
    assert props["position"]["enum"] == ["replace", "before", "after"]


# ---- 7) undo/redo exposed --------------------------------------------------------

def test_undo_redo_are_real_core_tools():
    from plugin.framework.tool import ToolBase
    from plugin.doc.undo import Redo, Undo

    for cls in (Undo, Redo):
        assert issubclass(cls, ToolBase)
        assert cls.tier == "core"
        assert cls.is_mutation is True
        assert "user" in cls.description.lower()  # the shared-stack caution must be in the description


def test_undo_counts_steps_and_reports_stack_state():
    from plugin.doc.undo import Undo

    um = MagicMock()
    um.isUndoPossible.side_effect = [True, True, False, False]
    um.isRedoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Undo().execute(ctx, steps=3)
    assert res["status"] == "ok" and res["undone"] == 2
    assert "can_undo" in res and res["can_redo"] is True  # promised by the tool description


def test_redo_counts_steps():
    from plugin.doc.undo import Redo

    um = MagicMock()
    um.isRedoPossible.side_effect = [True, False, True]
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Redo().execute(ctx, steps=2)
    assert res["status"] == "ok" and res["redone"] == 1


# ---- 8) recoverable error messages ----------------------------------------------

def test_apply_style_unknown_style_lists_names_and_suggests():
    from plugin.writer.styles import ApplyStyle

    fam = MagicMock()
    fam.hasByName.return_value = False
    fam.getElementNames.return_value = ["Heading 1", "Heading 2", "Text body", "Quotations"]
    ctx = MagicMock()
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    res = ApplyStyle().execute(ctx, style_name="heading 1", family="ParagraphStyles")
    assert res["status"] == "error"
    assert "Did you mean 'Heading 1'" in res["message"]
    assert "Text body" in res["message"]


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
