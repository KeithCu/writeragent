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
def test_dry_run_marshals_to_main_thread(monkeypatch):
    from plugin.writer.content import ApplyDocumentContent
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


def _next_to_match_found(*, at_paragraph_edge: bool, leftover: str = ""):
    """A match whose cursors all share one mock: not in a table cell, edges configurable."""
    found = MagicMock()
    cursor = found.getText.return_value.createTextCursorByRange.return_value
    cursor.getPropertyValue.return_value = None
    cursor.isStartOfParagraph.return_value = at_paragraph_edge
    cursor.isEndOfParagraph.return_value = at_paragraph_edge
    cursor.getString.return_value = leftover
    return found, cursor


def test_position_after_block_opens_a_paragraph_after_the_matched_one():
    """#59: a block imported at the match end split "... EM DOBRO" mid-paragraph and glued the
    first block onto it. It must go into a fresh paragraph after the matched one, and the empty
    paragraph the import leaves behind must be removed."""
    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
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
    from plugin.writer.content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=True, leftover="texto do usuario")
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
         patch.object(format_support, "html_fragment_contains_mixed_math", return_value=False), \
         patch.object(format_support, "insert_html_at_cursor"):
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["<p>novo</p>"], target="search", old_content="fim.", position="after")
    assert res["status"] == "ok" and "snapped_to_paragraph" not in res
    cursor.setString.assert_not_called()


def test_position_before_block_goes_to_paragraph_start_without_a_break():
    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
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
    from plugin.writer.content import ApplyDocumentContent

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
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
    from plugin.writer.content import ApplyDocumentContent

    found, _cursor = _next_to_match_found(at_paragraph_edge=True)
    with patch("plugin.writer.search.find_first_range", return_value=found), \
         patch("plugin.writer.content.collapsed_anchor", return_value=None), \
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


def test_apply_document_content_validate_accepts_string_or_array():
    """UNO tests pass one string. The schema was array-only, so validate rejected it.

    execute() still treats a string as one block and joins a list with newlines.
    A dict is not either shape.
    """
    from plugin.writer.content import ApplyDocumentContent

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
    from plugin.writer.content import _match_stripped_edges

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
    from plugin.writer.content import _grow_over_edge_space

    text = _StrText(body)
    assert _grow_over_edge_space(text.match(word), 1).getString() == taken


def test_grow_over_edge_space_is_a_no_op_without_edges():
    from plugin.writer.content import _grow_over_edge_space

    found = MagicMock()
    assert _grow_over_edge_space(found, 0) is found
    found.getText.assert_not_called()
