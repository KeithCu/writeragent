# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ApplyDocumentContent dry-run coverage. No LibreOffice."""
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.apply_document_content import ApplyDocumentContent

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
    from plugin.writer.apply_document_content import ApplyDocumentContent

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
    from plugin.writer.apply_document_content import ApplyDocumentContent
    res = ApplyDocumentContent().execute(_edit_ctx(), content=["x"], target="end", dry_run=True)
    assert res["status"] == "error" and "search" in res["message"]


@pytest.mark.timeout(5)
def test_dry_run_marshals_to_main_thread(monkeypatch):
    from plugin.writer.apply_document_content import ApplyDocumentContent
    import threading

    r1 = MagicMock()
    r1.getString.return_value = "clause 3.2 text"
    ctx = _edit_ctx()

    fake_bg = MagicMock()
    fake_bg.name = "worker-thread"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(threading, "main_thread", lambda: MagicMock())

    posts = []

    def fake_execute(fn, *args, **kwargs):
        posts.append(fn)
        # Mock executing the lambda and returning a tuple with result
        return fn()

    with patch("plugin.framework.queue_executor.execute_on_main_thread", fake_execute), \
         patch("plugin.writer.search.find_all_ranges", return_value=[r1]), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(ctx, content=["x"], target="search", old_content="clause 3.2", dry_run=True)

    assert res["status"] == "ok" and res["dry_run"] is True and res["count"] == 1
    assert len(posts) == 1


@pytest.mark.timeout(5)
def test_dry_run_honors_regex_via_the_same_matcher_as_the_edit():
    """A preview that uses a different matcher than the commit is worse than none: with
    regex=true, dry_run must route through find_ranges_regex_case with the RAW pattern."""
    from plugin.writer.apply_document_content import ApplyDocumentContent

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
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content="([a-", dry_run=True, regex=True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"


# ---- 5) position='before'/'after' contract ------------------------------------

def test_position_param_validation():
    from plugin.writer.apply_document_content import ApplyDocumentContent

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
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    # Not inside a table cell:
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False

    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
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
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>prefix</p>"], target="search", old_content="clause", position="before")
    assert res["status"] == "ok" and res["inserted"] is True and res["position"] == "before"
    ins.assert_called_once()
    found.getStart.assert_called()


def _next_to_match_found(*, at_paragraph_edge: bool, leftover: str = ""):
    """A match whose cursors all share one mock: not in a table cell, edges configurable."""
    found = MagicMock()
    cursor = found.getText.return_value.createTextCursorByRange.return_value
    cursor.getPropertyValue.return_value = None
    cursor.isStartOfParagraph.return_value = at_paragraph_edge
    cursor.isEndOfParagraph.return_value = at_paragraph_edge
    if leftover:
        cursor.getString.return_value = leftover
    else:
        cursor.getString.side_effect = ["", "\n", "\n", "\n"]
    return found, cursor


def test_position_after_block_opens_a_paragraph_after_the_matched_one():
    """#59: a block imported at the match end split "... EM DOBRO" mid-paragraph and glued the
    first block onto it. It must go into a fresh paragraph after the matched one, and the empty
    paragraph the import leaves behind must be removed."""
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_inline_at_cursor") as inline, \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>NOVO A</p><p>NOVO B</p>"], target="search", old_content="EM", position="after")

    assert res["status"] == "ok" and res["snapped_to_paragraph"] is True
    assert "paragraph that contains the match" in res["message"]
    cursor.gotoEndOfParagraph.assert_any_call(False)
    found.getText.return_value.insertControlCharacter.assert_called_once_with(cursor, 0, False)
    ins.assert_called_once()
    inline.assert_not_called()
    # Leftover empty paragraph: select the break before it and delete it.
    cursor.goLeft.assert_called_once_with(1, True)
    cursor.setString.assert_called_once_with("")


def test_position_after_block_keeps_a_non_empty_paragraph_after_import():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=True, leftover="texto do usuario")
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor"):
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["<p>novo</p>"], target="search", old_content="fim.", position="after")
    assert res["status"] == "ok" and "snapped_to_paragraph" not in res
    cursor.setString.assert_not_called()


def test_position_after_table_preserves_table():
    """Tip: stepping left from a paragraph following a table spans into the table; don't delete it."""
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    # 1st call: empty leftover paragraph; 2nd call: goLeft spanned table cell text.
    cursor.getString.side_effect = ["", "\nCell 1\n"]
    ctx = _edit_ctx()
    # Mock table count to increase from 0 to 1 so honesty check passes.
    ctx.doc.getTextTables.return_value.getCount.side_effect = [0, 1, 1]
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor"):
        res = ApplyDocumentContent().execute(
            ctx, content=["<table><tr><td>Cell 1</td></tr></table>"],
            target="search", old_content="world", position="after")

    assert res["status"] == "ok"
    cursor.goLeft.assert_called_once_with(1, True)
    cursor.setString.assert_not_called()


def test_table_insert_reports_failure_when_no_table_produced():
    """Tip: table insert must honestly report failure if table count did not increase."""
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    cursor.getString.side_effect = ["", "\nCell 1\n"]
    ctx = _edit_ctx()
    # Table count remains 0 before and after.
    ctx.doc.getTextTables.return_value.getCount.return_value = 0
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor"):
        res = ApplyDocumentContent().execute(
            ctx, content=["<table><tr><td>Cell 1</td></tr></table>"],
            target="search", old_content="world", position="after")

    assert res["status"] == "error"
    assert res.get("code") == "TABLE_INSERT_FAILED"
    assert "Table insert produced no table in document." in res.get("message", "")


def test_position_before_block_goes_to_paragraph_start_without_a_break():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["<h2>Nova</h2>"], target="search", old_content="meio", position="before")
    assert res["status"] == "ok" and res["snapped_to_paragraph"] is True
    cursor.gotoStartOfParagraph.assert_called_once_with(False)
    found.getText.return_value.insertControlCharacter.assert_not_called()
    ins.assert_called_once()


def test_position_before_inline_stays_at_the_exact_match_edge():
    """#59: plain text went through <p> wrapping and split the paragraph around the word."""
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_inline_at_cursor") as inline, \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["bem "], target="search", old_content="inteiro.", position="before")
    assert res["status"] == "ok" and "snapped_to_paragraph" not in res
    inline.assert_called_once()
    ins.assert_not_called()
    cursor.gotoStartOfParagraph.assert_not_called()


@pytest.mark.parametrize("content", ["Novo A\n\nNovo B", "Novo A\\n\\nNovo B"])
def test_position_after_plain_text_with_line_breaks_is_block(content):
    """Plain text with line breaks on the inline path went in as manual line breaks glued onto
    the matched paragraph ("inteiro.Novo A\\n\\nNovo B")."""
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, _cursor = _next_to_match_found(at_paragraph_edge=True)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_inline_at_cursor") as inline, \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=[content], target="search", old_content="inteiro.", position="after")
    assert res["status"] == "ok"
    ins.assert_called_once()
    inline.assert_not_called()


def test_position_after_rejects_math_and_table_cells():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

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
    from plugin.writer.apply_document_content import ApplyDocumentContent

    props = ApplyDocumentContent.parameters["properties"]
    assert props["position"]["enum"] == ["replace", "before", "after"]


def test_apply_document_content_validate_accepts_string_or_array():
    """UNO tests pass one string. The schema was array-only, so validate rejected it.

    execute() still treats a string as one block and joins a list with newlines.
    A dict is not either shape.
    """
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool = ApplyDocumentContent()
    samples = [
        (
            "<p>Intro</p>"
            '<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>q</mi></mrow></math>'
            "<p>Outro</p>"
        ),
        "<b>replaced</b>",
        "Line 1\nLine 2\n\nParagraph 2",
        "Line A\r\nLine B\nUNIQUE_CRLF_TEST",
        "zBertTicklez",
        "zNormaGlintez",
        "zGordonStumpz",
        ["<p>A</p>", "<p>B</p>"],
    ]
    for content in samples:
        ok, err = tool.validate(content=content, target="end")
        assert ok is True and err is None, (content, err)
    ok, err = tool.validate(content={"html": "<p>x</p>"}, target="end")
    assert ok is False
    assert err is not None and "Invalid type for content" in err



def test_truncated_flag_on_get_document_content():
    from unittest.mock import patch

    from plugin.writer import format as format_support
    from plugin.writer.get_document_content import GetDocumentContent

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
    from plugin.writer.get_document_content import GetDocumentContent

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
    from plugin.writer.get_document_content import GetDocumentContent

    doc = MagicMock()
    doc.getPropertyValue.return_value = True
    doc.getRedlines.return_value.getCount.return_value = 1
    text = MagicMock()
    doc.getText.return_value = text
    ctx = MagicMock()
    ctx.doc = doc
    ctx.ctx = MagicMock()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.get_document_content.collect_tracked_changes", return_value=[{"type": "Insert", "text": "x"}]), \
         patch("plugin.writer.format.document_to_content", return_value="body"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert res["tracked_changes"] == [{"type": "Insert", "text": "x"}]
    assert "tracked_changes_note" in res


def test_get_document_content_tracked_changes_disposal_propagates():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.get_document_content import GetDocumentContent

    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 1
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 3
    with patch("plugin.writer.get_document_content.collect_tracked_changes", side_effect=DocumentDisposedError("closed", object_type="Document")), \
         patch("plugin.writer.format.document_to_content", return_value="body"):
        with pytest.raises(DocumentDisposedError):
            GetDocumentContent().execute(ctx)


@pytest.mark.parametrize("old, new, expected", [
    # deletion: the word goes with the space it names (was "Primeiro  inteiro.")
    (" paragrafo", "", ("", 1)),
    ("paragrafo ", "", ("", 1)),
    (" paragrafo ", "", ("", 1)),
    (" x ", " ", ("", 1)),
    # replacement: drop the same edge the search dropped
    (" paragrafo inteiro", " inteiro", ("inteiro", 0)),
    # sloppy trailing space on old_content only: keep the replacement as sent
    ("Primeiro ", "Um", ("Um", 0)),
    ("foo", " bar", (" bar", 0)),
    # a symbol replaced by a space is a replacement, not a deletion ("bem_vindo" -> "bem vindo")
    ("_", " ", (" ", 0)),
    ("x", "", ("", 0)),
])
def test_match_stripped_edges(old, new, expected):
    from plugin.writer.apply_document_content import _match_stripped_edges

    assert _match_stripped_edges(old, new) == expected


class _StrRange:
    """A [a, b) range over a string, with the model-cursor moves _grow_over_edge_space uses."""

    def __init__(self, text, a, b):
        self.text, self.a, self.b = text, a, b

    def getText(self):
        return self.text

    def getStart(self):
        return _StrRange(self.text, self.a, self.a)

    def getEnd(self):
        return _StrRange(self.text, self.b, self.b)

    def getString(self):
        return self.text.s[self.a:self.b]

    def goLeft(self, n, expand):
        if self.a - n < 0:
            return False
        self.a -= n
        if not expand:
            self.b = self.a
        return True

    def goRight(self, n, expand):
        if self.b + n > len(self.text.s):
            return False
        self.b += n
        if not expand:
            self.a = self.b
        return True

    def gotoRange(self, other, expand):
        if expand:
            self.b = other.b
        else:
            self.a = self.b = other.a


class _StrText:
    def __init__(self, s):
        self.s = s

    def createTextCursorByRange(self, rng):
        return _StrRange(self, rng.a, rng.b)

    def match(self, word):
        at = self.s.index(word)
        return _StrRange(self, at, at + len(word))


@pytest.mark.parametrize("body, word, taken", [
    ("Primeiro paragrafo inteiro.", "paragrafo", " paragrafo"),
    # paragraph start: no space on the left, take the one on the right
    ("Primeiro inteiro.", "Primeiro", "Primeiro "),
    ("a\nx y", "x", "x "),
    # no space on either side: nothing to take
    ("(x)", "x", "x"),
])
def test_grow_over_edge_space_takes_one_separator(body, word, taken):
    from plugin.writer.apply_document_content import _grow_over_edge_space

    text = _StrText(body)
    assert _grow_over_edge_space(text.match(word), 1).getString() == taken


def test_grow_over_edge_space_is_a_no_op_without_edges():
    from plugin.writer.apply_document_content import _grow_over_edge_space

    found = MagicMock()
    assert _grow_over_edge_space(found, 0) is found
    found.getText.assert_not_called()


def test_all_matches_hoists_plain_text_conversion():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "old"
    r2.getString.return_value = "old"

    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(format_support, "plain_text_if_markup", return_value="hi") as mock_html_plain, \
         patch.object(format_support, "replace_single_range_with_content"):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content="<b>hi</b>", target="search", old_content="old", all_matches=True
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 2
    # plain_text_if_markup is called once for old_content and hoisted once for content (not per-match)
    assert mock_html_plain.call_count == 2
    assert mock_html_plain.call_args_list[1][0][0] == "<b>hi</b>"


def test_edge_spaces_with_use_preserve():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    found = MagicMock()
    found.getString.return_value = "paragrafo"

    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch("plugin.writer.apply_document_content.record_preserve_replace") as mock_preserve:
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx,
            old_content=" paragrafo",
            content=" &lt;b&gt;hi&lt;/b&gt;",
            target="search",
            position="replace",
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 1
    mock_preserve.assert_called_once()
    assert mock_preserve.call_args[0][3] == "<b>hi</b>"


def test_position_after_string_with_backslash_is_not_block():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_inline_at_cursor") as inline, \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content="<b>C:\\new</b>", target="search", old_content="inteiro.", position="after"
        )
    assert res["status"] == "ok"
    assert "snapped_to_paragraph" not in res
    inline.assert_called_once()
    ins.assert_not_called()


def test_target_end_entity_decoded_proposed_preview():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.edit_review import EditReviewSession

    ctx = _edit_ctx()
    with patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_to_plain_text", return_value="STRIPPED") as mock_html_plain, \
         patch.object(format_support, "insert_content_at_position"), \
         patch.object(EditReviewSession, "record_mutation") as mock_record:
        res = ApplyDocumentContent().execute(
            ctx, target="end", content="&lt;b&gt;hi&lt;/b&gt;"
        )
    assert res["status"] == "ok"
    mock_html_plain.assert_not_called()
    mock_record.assert_called_once()
    assert mock_record.call_args[1]["proposed_preview"] == "<b>hi</b>"


def test_shape_edit_leave_undo_context_failure_logged():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    shape = MagicMock()
    shape.Name = "TestShape"
    undo = MagicMock()
    undo.leaveUndoContext.side_effect = RuntimeError("leave undo failed")
    ctx.doc.getUndoManager.return_value = undo

    with patch("plugin.writer.search.find_first_range", return_value=None), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape", return_value=True), \
         patch("plugin.writer.apply_document_content.log.exception") as mock_log_exc:
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar"
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 1
    mock_log_exc.assert_called_once_with("apply_document_content: shape edit leaveUndoContext failed")


def test_content_list_item_must_be_string():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    with patch("plugin.writer.format.insert_content_at_position") as mock_insert:
        res = ApplyDocumentContent().execute(ctx, content=[None], target="end")
    assert res["status"] == "error"
    assert "content list items must be strings." in res["message"]
    mock_insert.assert_not_called()


def test_content_none_rejected():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    res = ApplyDocumentContent().execute(ctx, content=None, target="end")
    assert res["status"] == "error"
    assert "content must be a string or a list of strings." in res["message"]


@pytest.mark.parametrize("target", ["end", "beginning", "selection"])
@pytest.mark.parametrize("content", ["", "   ", "\n"])
def test_empty_insert_at_end_beginning_selection(target, content):
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    with patch("plugin.writer.format.insert_content_at_position") as mock_insert:
        res = ApplyDocumentContent().execute(ctx, content=content, target=target)
    assert res["status"] == "error"
    assert "content is empty; nothing was inserted." in res["message"]
    mock_insert.assert_not_called()


def test_empty_full_document_is_not_the_insert_error():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    with patch("plugin.writer.format.replace_full_document") as mock_replace:
        res = ApplyDocumentContent().execute(ctx, content="", target="full_document")
    assert res["status"] == "ok"
    assert "content is empty; nothing was inserted." not in (res.get("message") or "")
    mock_replace.assert_called_once()


def test_worker_cleanup_timeout_does_not_mask_the_edit():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    fake_worker = MagicMock()
    session_mock = MagicMock()

    def fake_execute_edit(_ctx, session_sink=None, **kwargs):
        if session_sink is not None:
            session_sink.append(session_mock)
        return {"status": "ok", "message": "Inserted content at end."}, None

    call_count = [0]

    def fake_execute_on_main(fn, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return fn()
        raise TimeoutError("cleanup timed out")

    with patch("plugin.writer.apply_document_content.threading.current_thread", return_value=fake_worker), \
         patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch.object(ApplyDocumentContent, "_execute_edit", side_effect=fake_execute_edit), \
         patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=fake_execute_on_main):
        res = ApplyDocumentContent().execute(ctx, content="test", target="end")

    assert res["status"] == "ok"
    assert res["message"].startswith("Inserted content at end.")


def test_outline_undo_says_when_rollback_fails():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc = MagicMock()
    doc.getUndoManager.return_value.isLocked.return_value = False
    session = MagicMock()
    session.changes = []

    def failing_run():
        raise RuntimeError("boom")

    with patch("plugin.writer.apply_document_content.close_surgical_context", return_value=False):
        _, err = ApplyDocumentContent()._outline_undo(doc, session, failing_run)
    assert err is not None
    assert err["code"] == "HYPERLINK_UPDATE_FAILED"
    assert "could not be rolled back" in err["message"]
    assert "boom" in err["message"]

    with patch("plugin.writer.apply_document_content.close_surgical_context", return_value=True):
        _, err2 = ApplyDocumentContent()._outline_undo(doc, session, failing_run)
    assert err2 is not None
    assert err2["code"] == "HYPERLINK_UPDATE_FAILED"
    assert "was rolled back" in err2["message"]
    assert "could not be rolled back" not in err2["message"]


def test_get_document_content_warns_when_redlines_are_not_listed():
    from plugin.writer.get_document_content import GetDocumentContent

    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 2
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.get_document_content.collect_tracked_changes", return_value=[]), \
         patch("plugin.writer.format.document_to_content", return_value="<p>body</p>"):
        res = GetDocumentContent().execute(ctx)
    assert res["status"] == "ok"
    assert "tracked_changes" not in res
    assert "could not list tracked changes" in res["warning"]
    assert "do not accept or reject" in res["warning"]


def test_get_document_content_redline_read_reraises_disposed():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.get_document_content import GetDocumentContent

    doc = MagicMock()
    doc.getRedlines.side_effect = type("DisposedException", (Exception,), {})("gone")
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_document_length.return_value = 10
    with patch("plugin.writer.format.document_to_content", return_value="<p>body</p>"):
        with pytest.raises(DocumentDisposedError):
            GetDocumentContent().execute(ctx)


def test_shape_edit_refuses_without_undo_context():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = True
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_first_range", return_value=None), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape") as replace:
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    replace.assert_not_called()

    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.doc.getUndoManager.return_value.enterUndoContext.side_effect = RuntimeError("no undo")
    with patch("plugin.writer.search.find_first_range", return_value=None), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape") as replace:
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar")
    assert res["code"] == "UNDO_UNAVAILABLE"
    replace.assert_not_called()


def test_shape_edit_enter_disposed_reraises():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    ctx.doc.getUndoManager.side_effect = type("DisposedException", (Exception,), {})("gone")
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_first_range", return_value=None), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape") as replace:
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, target="search", old_content="foo", content="bar")
    replace.assert_not_called()


def test_all_matches_failure_keeps_a_code_and_reraises_disposed():
    from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    r1, r2 = MagicMock(), MagicMock()
    calls = {"n": 0}

    def replace_found(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], None
        raise RuntimeError("boom")

    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_found):
        res = ApplyDocumentContent().execute(
            ctx, content="x", target="search", old_content="old", all_matches=True)
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert res["partial_failure"] is True
    assert "rolled back" in res["message"]

    calls["n"] = 0

    def replace_coded(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], None
        raise ToolExecutionError("coded", code="HYPERLINK_UPDATE_FAILED")

    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_coded):
        res = ApplyDocumentContent().execute(
            ctx, content="x", target="search", old_content="old", all_matches=True)
    assert res["code"] == "HYPERLINK_UPDATE_FAILED"
    assert res["partial_failure"] is True

    calls["n"] = 0

    def replace_link(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], None
        return None, {
            "status": "error",
            "code": "INVALID_PARAM",
            "message": "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
        }

    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_link):
        res = ApplyDocumentContent().execute(
            ctx, content="x", target="search", old_content="old", all_matches=True)
    assert res["code"] == "INVALID_PARAM"
    assert res["partial_failure"] is True

    def replace_disposed(*_args, **_kwargs):
        raise type("DisposedException", (Exception,), {})("gone")

    with patch("plugin.writer.search.find_all_ranges", return_value=[r1, r2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_disposed):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="x", target="search", old_content="old", all_matches=True)


def test_position_after_cell_probe_reraises_disposed():
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    cursor.getPropertyValue.side_effect = type("DisposedException", (Exception,), {})("gone")
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor") as ins:
        with pytest.raises(Exception) as exc_info:
            ApplyDocumentContent().execute(
                _edit_ctx(), content=["<p>x</p>"], target="search", old_content="c", position="after")
    assert "DisposedException" in type(exc_info.value).__name__
    ins.assert_not_called()


def test_emptied_table_delete_reraises_disposed():
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = _edit_ctx()
    found = MagicMock()
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[(MagicMock(), "T1")]), \
         patch("plugin.writer.apply_document_content.record_table_deletions",
                      side_effect=type("DisposedException", (Exception,), {})("gone")):
        with pytest.raises(Exception) as exc_info:
            ApplyDocumentContent().execute(
                ctx, target="search", old_content="x", content="")
    assert "DisposedException" in type(exc_info.value).__name__


@pytest.mark.timeout(5)
def test_dry_run_outline_capture_failure_is_not_a_missing_link():
    ctx = _edit_ctx()
    found = MagicMock()
    with patch("plugin.writer.search.find_all_ranges", return_value=[found]), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=RuntimeError("boom")):
        res = ApplyDocumentContent().execute(
            ctx, content="bar", target="search", old_content="clause", dry_run=True)
        assert res["status"] == "error"
        assert res["code"] == "HYPERLINK_UPDATE_FAILED"
        assert "Could not read the outline hyperlink" in res["message"]
        assert res.get("dry_run") is not True

        res_override = ApplyDocumentContent().execute(
            ctx, content="bar", target="search", old_content="clause",
            hyperlink_url="#1.Title|outline", dry_run=True)
        assert res_override["status"] == "error"
        assert res_override["code"] == "HYPERLINK_UPDATE_FAILED"
        assert "Could not read the outline hyperlink" in res_override["message"]

    disposed_exc = type("DisposedException", (Exception,), {})("gone")
    with patch("plugin.writer.search.find_all_ranges", return_value=[found]), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=disposed_exc):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause", dry_run=True)
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause",
                hyperlink_url="#1.Title|outline", dry_run=True)


def test_edit_outline_capture_failure_does_not_change_text():
    ctx = _edit_ctx()
    found = MagicMock()
    found.getString.return_value = "clause"
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=RuntimeError("boom")), \
         patch("plugin.writer.apply_document_content.record_preserve_replace") as preserve:
        res = ApplyDocumentContent().execute(
            ctx, content="bar", target="search", old_content="clause")
        assert res["status"] == "error"
        assert res["code"] == "HYPERLINK_UPDATE_FAILED"
        assert "Could not read the outline hyperlink" in res["message"]
        preserve.assert_not_called()

        res_override = ApplyDocumentContent().execute(
            ctx, content="bar", target="search", old_content="clause",
            hyperlink_url="#1.Title|outline")
        assert res_override["status"] == "error"
        assert res_override["code"] == "HYPERLINK_UPDATE_FAILED"
        assert "Could not read the outline hyperlink" in res_override["message"]
        preserve.assert_not_called()

    disposed_exc = type("DisposedException", (Exception,), {})("gone")
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=disposed_exc), \
         patch("plugin.writer.apply_document_content.record_preserve_replace") as preserve:
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause")
        preserve.assert_not_called()

        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause",
                hyperlink_url="#1.Title|outline")
        preserve.assert_not_called()

    # all_matches: patch find_all_ranges to [found]
    with patch("plugin.writer.search.find_all_ranges", return_value=[found]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=RuntimeError("boom")), \
         patch("plugin.writer.apply_document_content.record_preserve_replace") as preserve:
        res_all = ApplyDocumentContent().execute(
            ctx, all_matches=True, content="bar", target="search", old_content="clause")
        assert res_all["code"] == "HYPERLINK_UPDATE_FAILED"
        assert res_all["partial_failure"] is True
        assert res_all["replaced_count"] == 0
        assert "rolled back" in res_all["message"]
        preserve.assert_not_called()


def test_content_tool_discovery_and_modules():
    """Verify split modules declare tools correctly and register one instance each."""
    from plugin.framework.service import iter_named_concrete_subclasses
    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.writer import apply_document_content, get_document_content
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    assert ApplyDocumentContent.__module__ == "plugin.writer.apply_document_content"
    assert GetDocumentContent.__module__ == "plugin.writer.get_document_content"

    apply_tools = list(iter_named_concrete_subclasses(apply_document_content, ToolBase))
    assert apply_tools == [ApplyDocumentContent]

    get_tools = list(iter_named_concrete_subclasses(get_document_content, ToolBase))
    assert get_tools == [GetDocumentContent]

    reg = ToolRegistry(services={})
    reg.auto_discover(apply_document_content)
    reg.auto_discover(get_document_content)
    assert reg.get("apply_document_content") is not None
    assert reg.get("get_document_content") is not None

