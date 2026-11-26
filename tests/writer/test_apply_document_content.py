# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ApplyDocumentContent tool coverage (plugin/writer/apply_document_content.py). No LibreOffice."""
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.writer import html_import_styles
from plugin.writer.apply_document_content import ApplyDocumentContent
from plugin.writer.search import MatchMode
from plugin.tests.testing_utils import DisposedException

def _edit_ctx():
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.doc.getPropertyValue.return_value = False
    # dry_run sweeps shapes/comments; bare MagicMock enumerations never terminate.
    # Outline-hyperlink preview also walks portions; that stop is `enum_has_more`
    # (UNO True only), not this stub.
    ctx.doc.getDrawPage.return_value = []
    ctx.doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    return ctx


def _icu_rejects(ctx):
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 0
    return ctx

# ---- D1: dry_run ------------------------------------------------------------

@pytest.mark.timeout(5)
def test_dry_run_reports_matches_without_mutating():
    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "clause 3.2 text"
    r2.getString.return_value = "clause 3.2 again"
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(ctx, content=["x"], target="search", old_content="clause 3.2", dry_run=True)
    assert res["status"] == "ok" and res["dry_run"] is True and res["count"] == 2
    assert res["replaceable"][0]["location"] == "body"
    # No session/undo context opened for a dry run.
    ctx.doc.getUndoManager.assert_not_called()


def test_dry_run_requires_search_target():
    res = ApplyDocumentContent().execute(_edit_ctx(), content=["x"], target="end", dry_run=True)
    assert res["status"] == "error" and "search" in res["message"]


@pytest.mark.timeout(5)
def test_dry_run_marshals_to_main_thread(monkeypatch):
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

    with patch("plugin.framework.queue_executor.main.call", fake_execute), \
         patch("plugin.writer.search.find_literal_ranges", return_value=([r1], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(ctx, content=["x"], target="search", old_content="clause 3.2", dry_run=True)

    assert res["status"] == "ok" and res["dry_run"] is True and res["count"] == 1
    assert len(posts) == 1


@pytest.mark.timeout(5)
def test_dry_run_honors_regex_via_the_same_matcher_as_the_edit():
    """A preview that uses a different matcher than the commit is worse than none: with
    regex=true, dry_run must route through find_ranges_regex_case with the RAW pattern."""

    r = MagicMock()
    r.getString.return_value = "bravo charlie"
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_ranges_regex_case", return_value=[r]) as frc, \
         patch("plugin.writer.search.find_literal_ranges") as far, \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content=r"brav. charl.e", dry_run=True, regex=True)
    assert res["status"] == "ok" and res["count"] == 1
    far.assert_not_called()
    args = frc.call_args[0]
    assert args[1] == r"brav. charl.e" and args[2] is True  # raw pattern, regex on


def test_dry_run_invalid_regex():
    ctx = _icu_rejects(_edit_ctx())
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.html_import.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content="([a-", dry_run=True, regex=True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"


def test_dry_run_icu_only_regex_warns_on_shape_skip():
    ctx = _edit_ctx()
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 1
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.html_import.content_has_markup", return_value=False), \
         patch("plugin.writer.search.find_ranges_regex_case", return_value=[]):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content=r"\p{Lu}\w+", dry_run=True, regex=True)
    assert res["status"] == "ok"
    assert res.get("warning") == "shape/comment text not searched: pattern uses ICU-only syntax"


# ---- 5) position='before'/'after' contract ------------------------------------

def test_position_param_validation():
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
    """The Q8 happy path: collapsed cursor at the found range's edge, HTML import,
    result carries inserted=true/position and NO replaced_count."""

    from plugin.writer import html_import

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    # Not inside a table cell:
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False

    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            ctx, content=["<p>novo</p>"], target="search", old_content="clausula", position="after")

    assert res["status"] == "ok" and res["inserted"] is True and res["position"] == "after"
    assert "replaced_count" not in res
    ins.assert_called_once()
    # The match edge used must be getEnd() for 'after'.
    found.getStart.assert_called()


def test_position_before_inserts_at_match_start():
    from plugin.writer import html_import

    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value.getPropertyValue.return_value = None
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor") as ins:
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
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_inline_at_cursor") as inline, \
         patch.object(html_import, "insert_html_at_cursor") as ins:
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
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=True, leftover="texto do usuario")
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor", return_value=html_import_styles.WriteReport(tables_added=0)), \
         patch.object(html_import, "insert_html_after_paragraph", return_value=html_import_styles.WriteReport(tables_added=0)):
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["<p>novo</p>"], target="search", old_content="fim.", position="after")
    assert res["status"] == "ok" and "snapped_to_paragraph" not in res
    cursor.setString.assert_not_called()


def test_position_after_table_preserves_table():
    """Tip: stepping left from a paragraph following a table spans into the table; don't delete it."""
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    # 1st call: empty leftover paragraph; 2nd: goLeft spanned table cell; 3rd: goRight into next.
    cursor.getString.side_effect = ["", "\nCell 1\n", "next"]
    ctx = _edit_ctx()
    # Mock table count to increase from 0 to 1 so honesty check passes.
    ctx.doc.getTextTables.return_value.getCount.side_effect = [0, 1]
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor", return_value=html_import_styles.WriteReport(tables_added=1)):
        res = ApplyDocumentContent().execute(
            ctx, content=["<table><tr><td>Cell 1</td></tr></table>"],
            target="search", old_content="world", position="after")

    assert res["status"] == "ok"
    cursor.goLeft.assert_called_once_with(1, True)
    cursor.setString.assert_not_called()


@pytest.mark.parametrize("position", ["after", "before"])
def test_table_insert_reports_failure_when_no_table_produced(position):
    """Tip: table insert must honestly report failure if table count did not increase."""
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    cursor.getString.side_effect = ["", "\nCell 1\n", "next"]
    ctx = _edit_ctx()
    # Table count remains 0 before and after.
    ctx.doc.getTextTables.return_value.getCount.return_value = 0
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor", return_value=html_import_styles.WriteReport(tables_added=0)), \
         patch.object(html_import, "insert_html_after_paragraph", return_value=html_import_styles.WriteReport(tables_added=0)):
        res = ApplyDocumentContent().execute(
            ctx, content=["<table><tr><td>Cell 1</td></tr></table>"],
            target="search", old_content="world", position=position)

    assert res["status"] == "error"
    assert res.get("code") == "TABLE_INSERT_FAILED"
    assert "Table insert produced no table in document." in res.get("message", "")


def test_position_before_block_goes_to_paragraph_start_without_a_break():
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=["<h2>Nova</h2>"], target="search", old_content="meio", position="before")
    assert res["status"] == "ok" and res["snapped_to_paragraph"] is True
    cursor.gotoStartOfParagraph.assert_called_once_with(False)
    found.getText.return_value.insertControlCharacter.assert_not_called()
    ins.assert_called_once()


def test_position_before_inline_stays_at_the_exact_match_edge():
    """#59: plain text went through <p> wrapping and split the paragraph around the word."""
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_inline_at_cursor") as inline, \
         patch.object(html_import, "insert_html_at_cursor") as ins:
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
    from plugin.writer import html_import

    found, _cursor = _next_to_match_found(at_paragraph_edge=True)
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_inline_at_cursor") as inline, \
         patch.object(html_import, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content=[content], target="search", old_content="inteiro.", position="after")
    assert res["status"] == "ok"
    ins.assert_called_once()
    inline.assert_not_called()


def test_apply_document_content_validate_accepts_string_or_array():
    """UNO tests pass one string. The schema was array-only, so validate rejected it.

    execute() still treats a string as one block and joins a list with newlines.
    A dict is not either shape.
    """

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


def test_all_matches_replaces_every_range():
    from plugin.writer import html_import

    ctx = _edit_ctx()
    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "old"
    r2.getString.return_value = "old"

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "plain_text_if_markup", return_value="hi"), \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport()):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content="<b>hi</b>", target="search", old_content="old", all_matches=True
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 2


def test_search_replace_attaches_style_note_warning():
    from plugin.writer import html_import
    from tests.framework.suggested_call_access import assert_valid_suggested_call

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 2", "Standard"]
    fam.hasByName.side_effect = lambda n: n in ("Heading 2", "Standard")
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    found = MagicMock()
    found.getString.return_value = "old"
    post_range = MagicMock()

    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.find_all_ranges", return_value=[post_range]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["Heading2"])):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="Heading2">new</p>', target="search", old_content="old"
        )
    assert res["status"] == "ok"
    assert "warning" not in res
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert diag["matches_skipped"] == 1
    assert diag["matches_total"] == 1
    assert "in the host paragraph" in diag["message"]
    assert "apply_style restyles the whole paragraph containing the match." in diag["message"]
    assert "apply_style(target='search', old_content=...)" in diag["message"]
    assert "apply_style(target='search', old_content=...)" in res["message"]
    assert "suggested_call" in diag
    assert diag["suggested_call"] == {
        "tool": "apply_style",
        "arguments": {
            "target": "search",
            "old_content": "new",
            "style": "Heading 2",
            "case_sensitive": True,
        },
    }
    assert_valid_suggested_call(diag["suggested_call"])

    # When replace_single_range_with_content returns WriteReport(), warning is absent
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport()):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res_no_warn = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="Heading2">new</p>', target="search", old_content="old"
        )
    assert res_no_warn["status"] == "ok"
    assert "warning" not in res_no_warn
    assert "warnings" not in res_no_warn


def test_all_matches_reports_skip_count_in_warning():
    from plugin.writer import html_import
    from tests.framework.suggested_call_access import assert_valid_suggested_call

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 2", "Standard"]
    fam.hasByName.side_effect = lambda n: n in ("Heading 2", "Standard")
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "old"
    r2.getString.return_value = "old"

    # First match styled (WriteReport()), second match partial (WriteReport(style_skips=..., style_tokens=...))
    side_effects = [
        html_import_styles.WriteReport(),
        html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["Heading2"]),
    ]
    post1, post2 = MagicMock(), MagicMock()

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
         patch("plugin.writer.search.find_all_ranges", return_value=[post1, post2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "plain_text_if_markup", return_value="new"), \
         patch.object(html_import, "replace_single_range_with_content", side_effect=side_effects):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="Heading2">new</p>', target="search", old_content="old", all_matches=True
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 2
    assert "warning" not in res
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert diag["matches_skipped"] == 1
    assert diag["matches_total"] == 2
    assert "in the host paragraph" in diag["message"]
    assert "apply_style restyles the whole paragraph containing the match." in diag["message"]
    assert "data-lo-style='Heading2' was not applied to 1 of 2 matches because they do not cover whole paragraphs" in diag["message"]
    assert "data-lo-style='Heading2' was not applied to 1 of 2 matches because they do not cover whole paragraphs" in res["message"]
    assert "suggested_call" in diag
    assert diag["suggested_call"] == {
        "tool": "apply_style",
        "arguments": {
            "target": "search",
            "old_content": "new",
            "style": "Heading 2",
            "case_sensitive": True,
            "all_matches": True,
        },
    }
    assert_valid_suggested_call(diag["suggested_call"])


def test_suggested_call_omitted_when_style_unresolvable():
    from plugin.writer import html_import

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    fam.hasByName.side_effect = lambda n: n == "Standard"
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    found = MagicMock()
    found.getString.return_value = "old"

    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["UnknownStyle"])):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="UnknownStyle">new</p>', target="search", old_content="old"
        )
    assert res["status"] == "ok"
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "suggested_call" not in diag


def test_suggested_call_omitted_when_text_has_newlines():
    from plugin.writer import html_import

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 2", "Standard"]
    fam.hasByName.side_effect = lambda n: n in ("Heading 2", "Standard")
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    found = MagicMock()
    found.getString.return_value = "old"
    post_range = MagicMock()

    content = '<p data-lo-style="Heading2">a</p><p data-lo-style="Heading2">b</p>'
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.find_all_ranges", return_value=[post_range]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["Heading2"])):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content=content, target="search", old_content="old"
        )
    assert res["status"] == "ok"
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "suggested_call" not in diag


def test_suggested_call_omitted_for_selection_target():
    from plugin.writer import html_import

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 2", "Standard"]
    fam.hasByName.side_effect = lambda n: n in ("Heading 2", "Standard")
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam

    with patch("plugin.writer.apply_document_content.request_insert_range", return_value=(None, False)), \
         patch.object(html_import, "insert_content_at_position", return_value=html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["Heading2"])):
        res = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="Heading2">new</p>', target="selection"
        )
    assert res["status"] == "ok"
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "suggested_call" not in diag


def test_suggested_call_omitted_when_post_match_count_mismatches():
    from plugin.writer import html_import

    ctx = _edit_ctx()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 2", "Standard"]
    fam.hasByName.side_effect = lambda n: n in ("Heading 2", "Standard")
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    found = MagicMock()
    found.getString.return_value = "old"

    # Post-write search finds 2 matches instead of expected 1
    m1, m2 = MagicMock(), MagicMock()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.find_all_ranges", return_value=[m1, m2]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links, \
         patch.object(html_import, "replace_single_range_with_content", return_value=html_import_styles.WriteReport(style_skips=["STYLE_IGNORED_PARTIAL_MATCH"], style_tokens=["Heading2"])):
        cap_links.return_value.links = []
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content='<p data-lo-style="Heading2">new</p>', target="search", old_content="old"
        )
    assert res["status"] == "ok"
    assert "warnings" in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "suggested_call" not in diag


def _make_edit_params(**kwargs: Any):
    from plugin.writer.apply_document_content import EditParams

    defaults: dict[str, Any] = {
        "content": '<p data-lo-style="Heading2">new</p>',
        "plain": "new",
        "use_preserve": False,
        "has_table": False,
        "target": "search",
        "position": "replace",
        "occurrence": None,
        "all_matches": False,
        "track_reviewable": False,
    }
    defaults.update(kwargs)
    return EditParams(**defaults)


def _mock_doc_with_styles(styles=("Heading 2", "Standard")):
    doc = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = list(styles)
    fam.hasByName.side_effect = lambda n: n in styles
    doc.getStyleFamilies.return_value.getByName.return_value = fam
    return doc


def test_partial_match_style_suggestion_happy_path_single():
    from plugin.writer.apply_document_content import _partial_match_style_suggestion
    from tests.framework.suggested_call_access import assert_valid_suggested_call

    doc = _mock_doc_with_styles()
    params = _make_edit_params()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([MagicMock()], MatchMode(True, False))):
        suggestion = _partial_match_style_suggestion(doc, params, "Heading2", 1)

    assert suggestion == {
        "tool": "apply_style",
        "arguments": {
            "target": "search",
            "old_content": "new",
            "style": "Heading 2",
            "case_sensitive": True,
        },
    }
    assert_valid_suggested_call(suggestion)


def test_partial_match_style_suggestion_happy_path_all_matches():
    from plugin.writer.apply_document_content import _partial_match_style_suggestion
    from tests.framework.suggested_call_access import assert_valid_suggested_call

    doc = _mock_doc_with_styles()
    params = _make_edit_params(all_matches=True)
    with patch("plugin.writer.search.find_all_ranges", return_value=[MagicMock(), MagicMock()]):
        suggestion = _partial_match_style_suggestion(doc, params, "Heading2", 2)

    assert suggestion == {
        "tool": "apply_style",
        "arguments": {
            "target": "search",
            "old_content": "new",
            "style": "Heading 2",
            "case_sensitive": True,
            "all_matches": True,
        },
    }
    assert_valid_suggested_call(suggestion)


def test_partial_match_style_suggestion_gate_selection_target():
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = _mock_doc_with_styles()
    params = _make_edit_params(target="selection")
    assert _partial_match_style_suggestion(doc, params, "Heading2", 1) is None


@pytest.mark.parametrize("plain", ["a\nb", "a\rb", ""])
def test_partial_match_style_suggestion_gate_newlines_or_empty(plain: str):
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = _mock_doc_with_styles()
    params = _make_edit_params(plain=plain)
    assert _partial_match_style_suggestion(doc, params, "Heading2", 1) is None


@pytest.mark.parametrize("token", [None, ""])
def test_partial_match_style_suggestion_gate_missing_style_token(token: str | None):
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = _mock_doc_with_styles()
    params = _make_edit_params()
    assert _partial_match_style_suggestion(doc, params, token, 1) is None


def test_partial_match_style_suggestion_gate_unresolvable_style_fallback():
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = _mock_doc_with_styles()
    params = _make_edit_params()
    assert _partial_match_style_suggestion(doc, params, "UnresolvedStyle", 1) is None


def test_partial_match_style_suggestion_gate_style_family_unavailable():
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = MagicMock()
    doc.getStyleFamilies.side_effect = RuntimeError("family lookup failed")
    params = _make_edit_params()
    assert _partial_match_style_suggestion(doc, params, "Heading2", 1) is None


@pytest.mark.parametrize("find_mock", [
    [MagicMock(), MagicMock()],  # too many matches
    [],                          # no matches
    ValueError("search error"),  # search exception
])
def test_partial_match_style_suggestion_gate_match_count_mismatch(find_mock: Any):
    from plugin.writer.apply_document_content import _partial_match_style_suggestion

    doc = _mock_doc_with_styles()
    params = _make_edit_params()
    side_effect = find_mock if isinstance(find_mock, Exception) else None
    return_value = None if side_effect is not None else find_mock

    with patch("plugin.writer.search.find_literal_ranges", return_value=(return_value, MatchMode(True, False)) if side_effect is None else None, side_effect=side_effect):
        assert _partial_match_style_suggestion(doc, params, "Heading2", 1) is None


def test_edge_spaces_with_use_preserve():
    ctx = _edit_ctx()
    found = MagicMock()
    found.getString.return_value = "paragrafo"

    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
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
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_inline_at_cursor") as inline, \
         patch.object(html_import, "insert_html_at_cursor") as ins:
        res = ApplyDocumentContent().execute(
            _edit_ctx(), content="<b>C:\\new</b>", target="search", old_content="inteiro.", position="after"
        )
    assert res["status"] == "ok"
    assert "snapped_to_paragraph" not in res
    inline.assert_called_once()
    ins.assert_not_called()


def test_target_end_entity_decoded_proposed_preview():
    from plugin.writer import html_import
    from plugin.writer.edit_review import EditReviewSession

    ctx = _edit_ctx()
    with patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_content_at_position", return_value=html_import_styles.WriteReport()), \
         patch.object(EditReviewSession, "record_mutation", return_value=html_import_styles.WriteReport()) as mock_record:
        res = ApplyDocumentContent().execute(
            ctx, target="end", content="&lt;b&gt;hi&lt;/b&gt;"
        )
    assert res["status"] == "ok"
    mock_record.assert_called_once()
    assert mock_record.call_args[1]["proposed_preview"] == "<b>hi</b>"


def test_shape_edit_leave_undo_context_failure_does_not_fail_edit():
    ctx = _edit_ctx()
    shape = MagicMock()
    shape.Name = "TestShape"
    undo = MagicMock()
    undo.leaveUndoContext.side_effect = RuntimeError("leave undo failed")
    ctx.doc.getUndoManager.return_value = undo

    with patch("plugin.writer.search.find_literal_ranges", return_value=([], MatchMode(False, False))), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape", return_value=True):
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar"
        )
    assert res["status"] == "ok"
    assert res["replaced_count"] == 1


def test_content_list_item_must_be_string():
    ctx = _edit_ctx()
    with patch("plugin.writer.html_import.insert_content_at_position") as mock_insert:
        res = ApplyDocumentContent().execute(ctx, content=[None], target="end")
    assert res["status"] == "error"
    assert "content list items must be strings." in res["message"]
    mock_insert.assert_not_called()


def test_content_none_rejected():
    ctx = _edit_ctx()
    res = ApplyDocumentContent().execute(ctx, content=None, target="end")
    assert res["status"] == "error"
    assert "content must be a string or a list of strings." in res["message"]


@pytest.mark.parametrize("target", ["end", "beginning", "selection"])
@pytest.mark.parametrize("content", ["", "   ", "\n"])
def test_empty_insert_at_end_beginning_selection(target, content):
    ctx = _edit_ctx()
    with patch("plugin.writer.html_import.insert_content_at_position") as mock_insert:
        res = ApplyDocumentContent().execute(ctx, content=content, target=target)
    assert res["status"] == "error"
    assert "content is empty; nothing was inserted." in res["message"]
    mock_insert.assert_not_called()


def test_empty_full_document_is_not_the_insert_error():
    ctx = _edit_ctx()
    with patch("plugin.writer.html_import.replace_full_document") as mock_replace:
        mock_replace.return_value = html_import_styles.WriteReport()
        res = ApplyDocumentContent().execute(ctx, content="", target="full_document")
    assert res["status"] == "ok"
    assert "content is empty; nothing was inserted." not in (res.get("message") or "")
    mock_replace.assert_called_once()


def test_worker_cleanup_timeout_does_not_mask_the_edit():
    ctx = _edit_ctx()
    session_mock = MagicMock()

    call_count = [0]

    def fake_execute_on_main(fn, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return fn()
        raise TimeoutError("cleanup timed out")

    with patch("plugin.framework.queue_executor.main.is_current", return_value=False), \
         patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.apply_document_content.EditReviewSession", return_value=session_mock), \
         patch.object(
             ApplyDocumentContent,
             "_dispatch_edit",
             return_value={"status": "ok", "message": "Inserted content at end."},
         ), \
         patch("plugin.framework.queue_executor.main.call", side_effect=fake_execute_on_main):
        res = ApplyDocumentContent().execute(ctx, content="test", target="end")

    assert res["status"] == "ok"
    assert res["message"].startswith("Inserted content at end.")
    assert call_count[0] == 2


def test_apply_document_content_releases_session_on_mid_edit_failure():
    ctx = _edit_ctx()
    session_mock = MagicMock()

    tool = ApplyDocumentContent()
    with patch("plugin.writer.apply_document_content.EditReviewSession", return_value=session_mock), \
         patch.object(
             ApplyDocumentContent,
             "_dispatch_edit",
             side_effect=RuntimeError("boom mid-edit"),
         ), \
         patch.object(tool, "_release_review_session") as mock_release:
        with pytest.raises(RuntimeError, match="boom mid-edit"):
            tool.execute(ctx, content="test", target="end")
        mock_release.assert_called_once_with(session_mock)


def test_param_errors_do_not_construct_review_session():
    ctx = _edit_ctx()
    with patch("plugin.writer.apply_document_content.EditReviewSession") as session_cls:
        tool = ApplyDocumentContent()
        res = tool.execute(ctx, content=[None], target="end")
        assert res["status"] == "error"
        session_cls.assert_not_called()

        _icu_rejects(ctx)
        res = tool.execute(
            ctx,
            content="x",
            target="search",
            old_content="([a-",
            regex=True,
        )
        assert res.get("code") == "INVALID_REGEX"
        session_cls.assert_not_called()


@pytest.mark.parametrize(
    "expected_method,execute_kwargs",
    [
        ("_edit_full_document", {"target": "full_document", "content": "body"}),
        ("_edit_insert_at", {"target": "end", "content": "tail"}),
        ("_edit_insert_at", {"target": "beginning", "content": "head"}),
        ("_edit_selection", {"target": "selection", "content": "sel"}),
        ("_edit_search", {"target": "search", "content": "new", "old_content": "old"}),
        # Unknown target strings still dispatch through the search body (legacy callers).
        ("_edit_search", {"target": "legacy", "content": "new", "old_content": "old"}),
    ],
    ids=["full_document", "end", "beginning", "selection", "search", "unknown_target"],
)
def test_apply_document_content_routes_targets_to_edit_helpers(expected_method, execute_kwargs):
    ctx = _edit_ctx()
    routed: list[str] = []

    def _route_stub(*args, **kwargs):
        routed.append(expected_method)
        return {"status": "ok", "message": "stub"}

    def _forbidden(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"{name} should not run")
        return _fail

    tool = ApplyDocumentContent()
    patches = {
        "_edit_full_document": patch.object(tool, "_edit_full_document"),
        "_edit_insert_at": patch.object(tool, "_edit_insert_at"),
        "_edit_selection": patch.object(tool, "_edit_selection"),
        "_edit_search": patch.object(tool, "_edit_search"),
    }
    with patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.EditReviewSession", return_value=MagicMock()):
        for name, patcher in patches.items():
            if name == expected_method:
                patcher.start().side_effect = _route_stub
            else:
                patcher.start().side_effect = _forbidden(name)
        try:
            res = tool.execute(ctx, **execute_kwargs)
        finally:
            for patcher in patches.values():
                patcher.stop()
    assert res["status"] == "ok"
    assert routed == [expected_method]


def test_outline_undo_reraises_disposal_on_mutate_error():
    """commit_or_rollback re-raises disposal today; this pins _outline_undo if mutate_error is set."""
    from plugin.doc.compound_undo import UndoOutcome

    doc = MagicMock()
    session = MagicMock()
    session.changes = []
    disposed = DisposedException("gone")
    outcome = UndoOutcome(rolled_back=True, mutate_error=disposed)

    with patch("plugin.writer.apply_document_content.commit_or_rollback", return_value=outcome):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent()._outline_undo(doc, session, lambda: [])


def test_apply_document_content_main_thread_uses_is_current():
    from plugin.writer.apply_document_content import ApplyDocumentContent, _run_on_main_thread

    tool = ApplyDocumentContent()
    # When is_current returns True, is_async returns False (when wait mode is disabled globally)
    with patch("plugin.framework.queue_executor.main.is_current", return_value=True), \
         patch.object(tool, "_wait_enabled_globally", return_value=False):
        assert tool.is_async() is False

    # When is_current returns False, is_async returns True
    with patch("plugin.framework.queue_executor.main.is_current", return_value=False), \
         patch.object(tool, "_wait_enabled_globally", return_value=False):
        assert tool.is_async() is True

    # _run_on_main_thread calls fn directly when is_current() is True
    called = []
    with patch("plugin.framework.queue_executor.main.is_current", return_value=True), \
         patch("plugin.framework.queue_executor.main.call") as mock_exec:
        result = _run_on_main_thread(lambda: called.append(True) or 42)
        assert result == 42
        assert called == [True]
        mock_exec.assert_not_called()

    # When is_current() is False, _run_on_main_thread marshals to main.call
    with patch("plugin.framework.queue_executor.main.is_current", return_value=False), \
         patch("plugin.framework.queue_executor.main.call", return_value=99) as mock_exec:
        result = _run_on_main_thread(lambda: 100)
        assert result == 99
        mock_exec.assert_called_once()


def test_outline_undo_says_when_rollback_fails():
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr
    session = MagicMock()
    session.changes = ["change1"]

    def failing_run():
        session.changes.append("change2")
        raise RuntimeError("boom")

    mgr.getAllUndoActionTitles.side_effect = [(), ("Agent Edit",)]
    mgr.undo.side_effect = RuntimeError("undo failed")
    with patch("plugin.writer.apply_document_content.next_agent_edit_undo_title", return_value="Agent Edit"):
        with pytest.raises(ToolExecutionError) as exc_info:
            ApplyDocumentContent()._outline_undo(doc, session, failing_run)
    assert exc_info.value.code == "HYPERLINK_UPDATE_FAILED"
    assert "could not be rolled back" in str(exc_info.value)
    assert "boom" in str(exc_info.value)

    mgr.undo.side_effect = None
    mgr.getAllUndoActionTitles.side_effect = [(), ("Agent Edit",)]
    with patch("plugin.writer.apply_document_content.next_agent_edit_undo_title", return_value="Agent Edit"):
        with pytest.raises(ToolExecutionError) as exc_info2:
            ApplyDocumentContent()._outline_undo(doc, session, failing_run)
    assert exc_info2.value.code == "HYPERLINK_UPDATE_FAILED"
    assert "was rolled back" in str(exc_info2.value)
    assert "could not be rolled back" not in str(exc_info2.value)


def test_outline_undo_propagates_disposed_document():
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Agent Edit",)]
    doc.getUndoManager.return_value = mgr
    session = MagicMock()
    session.changes = []

    def disposed_run():
        raise DisposedException("gone")

    with patch("plugin.writer.apply_document_content.next_agent_edit_undo_title", return_value="Agent Edit"):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent()._outline_undo(doc, session, disposed_run)
    mgr.undo.assert_called_once()


def test_shape_edit_refuses_without_undo_context():
    ctx = _edit_ctx()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = True
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_literal_ranges", return_value=([], MatchMode(False, False))), \
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
    with patch("plugin.writer.search.find_literal_ranges", return_value=([], MatchMode(False, False))), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape") as replace:
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar")
    assert res["code"] == "UNDO_UNAVAILABLE"
    replace.assert_not_called()


def test_shape_edit_enter_disposed_reraises():
    ctx = _edit_ctx()
    ctx.doc.getUndoManager.side_effect = DisposedException("gone")
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_literal_ranges", return_value=([], MatchMode(False, False))), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape") as replace:
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, target="search", old_content="foo", content="bar")
    replace.assert_not_called()


def test_shape_edit_rolls_back_when_replace_returns_false():
    ctx = _edit_ctx()
    mgr = ctx.doc.getUndoManager.return_value
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("WriterAgent edit#1",)]
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_literal_ranges", return_value=([], MatchMode(False, False))), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape), \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape", return_value=False), \
         patch("plugin.writer.apply_document_content.next_agent_edit_undo_title", return_value="WriterAgent edit#1"):
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="foo", content="bar")
    assert res["status"] == "error"
    assert res["replaced_count"] == 0
    mgr.undo.assert_called_once()


def test_shape_edit_regex_shares_stripped_pattern():
    ctx = _edit_ctx()
    shape = MagicMock()
    shape.Name = "Box"
    with patch("plugin.writer.search.find_ranges_regex_case", return_value=None), \
         patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape) as mock_find_shape, \
         patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False), \
         patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.apply_document_content.replace_text_in_shape", return_value=True) as mock_replace:
        res = ApplyDocumentContent().execute(
            ctx, target="search", old_content="hel+o  x", content="BAR", regex=True)

    assert res["status"] == "ok"
    assert res["replaced_count"] == 1
    mock_find_shape.assert_called_once_with(
        ctx.doc, "hel+o  x", use_regex=True, case_sensitive=False
    )
    mock_replace.assert_called_once_with(
        shape, "hel+o  x", "BAR", case_sensitive=False, use_regex=True
    )
    assert mock_replace.call_args[0][1] != "hel+o x"


def test_all_matches_failure_keeps_a_code_and_reraises_disposed():
    ctx = _edit_ctx()
    r1, r2 = MagicMock(), MagicMock()
    calls = {"n": 0}

    def replace_found(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        raise RuntimeError("boom")

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
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
            return []
        raise ToolExecutionError("coded", code="HYPERLINK_UPDATE_FAILED")

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
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
            return []
        raise ToolExecutionError(
            "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
            code="INVALID_PARAM",
        )

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_link):
        res = ApplyDocumentContent().execute(
            ctx, content="x", target="search", old_content="old", all_matches=True)
    assert res["code"] == "INVALID_PARAM"
    assert res["partial_failure"] is True

    def replace_disposed(*_args, **_kwargs):
        raise DisposedException("gone")

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1, r2], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch.object(ApplyDocumentContent, "_replace_found", side_effect=replace_disposed):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="x", target="search", old_content="old", all_matches=True)


def test_all_matches_outline_update_failure_reports_hyperlink_code():
    from plugin.writer.hyperlink_fixup import OutlineLink, OutlineSnapshot

    ctx = _edit_ctx()
    r1 = MagicMock()
    r1.getString.return_value = "old"

    snapshot = OutlineSnapshot(
        links=[OutlineLink("#1.old|outline", "old", "old")],
        prefix="X",
        suffix="",
        matched="old",
        single_paragraph=True,
    )

    with patch("plugin.writer.search.find_literal_ranges", return_value=([r1], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", return_value=snapshot), \
         patch("plugin.writer.html_import.replace_single_range_with_content", return_value=html_import_styles.WriteReport()), \
         patch("plugin.writer.apply_document_content.next_agent_edit_undo_title", return_value="Agent Edit"):
        ctx.doc.getUndoManager.return_value.getAllUndoActionTitles.side_effect = [(), (), ("Agent Edit",)]
        res = ApplyDocumentContent().execute(
            ctx, content="new", target="search", old_content="old", all_matches=True
        )

    assert res["status"] == "error"
    assert res["code"] == "HYPERLINK_UPDATE_FAILED"
    assert res["partial_failure"] is True
    assert "rolled back" in res["message"]
    assert "Could not locate the replaced text" in res["message"]


def test_position_after_cell_probe_reraises_disposed():
    from plugin.writer import html_import

    found, cursor = _next_to_match_found(at_paragraph_edge=False)
    cursor.getPropertyValue.side_effect = DisposedException("gone")
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch.object(html_import, "insert_html_at_cursor") as ins:
        with pytest.raises(Exception) as exc_info:
            ApplyDocumentContent().execute(
                _edit_ctx(), content=["<p>x</p>"], target="search", old_content="c", position="after")
    assert "DisposedException" in type(exc_info.value).__name__
    ins.assert_not_called()


def test_emptied_table_delete_reraises_disposed():
    ctx = _edit_ctx()
    found = MagicMock()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[(MagicMock(), "T1")]), \
         patch("plugin.writer.apply_document_content.record_table_deletions",
                      side_effect=DisposedException("gone")):
        with pytest.raises(Exception) as exc_info:
            ApplyDocumentContent().execute(
                ctx, target="search", old_content="x", content="")
    assert "DisposedException" in type(exc_info.value).__name__


@pytest.mark.timeout(5)
def test_dry_run_outline_capture_failure_is_not_a_missing_link():
    ctx = _edit_ctx()
    found = MagicMock()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False), \
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

    disposed_exc = DisposedException("gone")
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", side_effect=disposed_exc):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause", dry_run=True)
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(
                ctx, content="bar", target="search", old_content="clause",
                hyperlink_url="#1.Title|outline", dry_run=True)


@pytest.mark.timeout(5)
@pytest.mark.parametrize("empty_content", ["", [], [""], ["", "   "]])
def test_dry_run_empty_replacement_omits_outline_rewrite_fields(empty_content):
    from plugin.writer.hyperlink_fixup import OutlineLink, OutlineSnapshot

    ctx = _edit_ctx()
    found = MagicMock()
    snapshot = OutlineSnapshot(
        links=[OutlineLink(url="#1.clause|outline", name="1.clause", target="")],
        prefix="",
        suffix="",
        matched="clause",
        single_paragraph=True,
    )
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.html_import.content_has_markup", return_value=False), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", return_value=snapshot) as mock_capture:
        res = ApplyDocumentContent().execute(
            ctx,
            content=empty_content,
            target="search",
            old_content="clause",
            occurrence=0,
            dry_run=True,
        )
    assert res["status"] == "ok"
    assert res["dry_run"] is True
    assert res["count"] == 1
    mock_capture.assert_not_called()
    assert len(res["replaceable"]) == 1
    for key in ("hyperlink_url", "hyperlink_url_after", "hyperlink_updated", "hyperlinks"):
        assert key not in res["replaceable"][0]
        assert key not in res["selected_match"]


def test_edit_outline_capture_failure_does_not_change_text():
    ctx = _edit_ctx()
    found = MagicMock()
    found.getString.return_value = "clause"
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
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

    disposed_exc = DisposedException("gone")
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
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
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
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


# ---- undo entry, table verification, regex validation ----------------------

def test_outline_undo_disposed_undo_manager_reraises():
    doc = MagicMock()
    doc.getUndoManager.side_effect = DisposedException("gone")
    session = MagicMock()
    session.changes = []
    with pytest.raises(DocumentDisposedError):
        ApplyDocumentContent()._outline_undo(doc, session, lambda: [])


def test_all_matches_disposed_undo_manager_reraises():
    ctx = _edit_ctx()
    ctx.doc.getUndoManager.side_effect = DisposedException("gone")
    with patch("plugin.writer.search.find_literal_ranges", return_value=([MagicMock()], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]):
        with pytest.raises(DocumentDisposedError):
            ApplyDocumentContent().execute(ctx, content="x", target="search", old_content="old", all_matches=True)


@pytest.mark.parametrize(
    ("target", "extra_kwargs", "patches"),
    [
        pytest.param(
            "end",
            {},
            [
                ("plugin.writer.html_import.insert_content_at_position", {"return_value": html_import_styles.WriteReport(tables_added=0)}),
                ("plugin.writer.apply_document_content.collapsed_anchor", {"return_value": None}),
            ],
            id="end",
        ),
        pytest.param(
            "selection",
            {},
            [
                ("plugin.writer.apply_document_content.request_insert_range", {"return_value": (None, False)}),
                ("plugin.writer.html_import.insert_content_at_position", {"return_value": html_import_styles.WriteReport(tables_added=0)}),
            ],
            id="selection",
        ),
        pytest.param(
            "search",
            {"old_content": "old"},
            [
                ("plugin.writer.search.find_literal_ranges", {"return_value": ([MagicMock()], MatchMode(True, False))}),
                ("plugin.writer.apply_document_content.collapsed_anchor", {"return_value": None}),
                ("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", {"return_value": []}),
                ("plugin.writer.html_import.replace_single_range_with_content", {"return_value": html_import_styles.WriteReport(tables_added=0)}),
            ],
            id="search",
        ),
        pytest.param(
            "search",
            {"old_content": "old", "all_matches": True},
            [
                ("plugin.writer.search.find_literal_ranges", {"return_value": ([MagicMock()], MatchMode(True, False))}),
                ("plugin.writer.apply_document_content.collapsed_anchor", {"return_value": None}),
                ("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", {"return_value": []}),
                ("plugin.writer.html_import.replace_single_range_with_content", {"return_value": html_import_styles.WriteReport(tables_added=0)}),
            ],
            id="all_matches",
        ),
    ],
)
def test_table_insert_fails_when_the_doc_already_has_a_table(target, extra_kwargs, patches):
    from contextlib import ExitStack

    ctx = _edit_ctx()
    ctx.doc.getTextTables.return_value.getCount.side_effect = [1, 1]
    with ExitStack() as stack:
        for p_target, p_kwargs in patches:
            stack.enter_context(patch(p_target, **p_kwargs))
        if target == "search":
            cap_links = stack.enter_context(patch("plugin.writer.apply_document_content.capture_outline_hyperlinks"))
            cap_links.return_value.links = []
            cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content="<table><tr><td>X</td></tr></table>", target=target, **extra_kwargs)
    assert res["status"] == "error"
    assert res["code"] == "TABLE_INSERT_FAILED"
    if extra_kwargs.get("all_matches"):
        assert "Table insert produced no table in document." in res["message"]
    else:
        assert res["message"] == "Table insert produced no table in document."


def test_table_insert_failed_inside_outline_undo_keeps_code():
    ctx = _edit_ctx()
    with patch("plugin.writer.search.find_literal_ranges", return_value=([MagicMock()], MatchMode(True, False))), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=None), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch("plugin.writer.html_import.replace_single_range_with_content",
               return_value=html_import_styles.WriteReport(tables_added=0)), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as cap_links:
        cap_links.return_value.links = [MagicMock()]
        cap_links.return_value.preserve_url = None
        res = ApplyDocumentContent().execute(
            ctx, content="<table><tr><td>X</td></tr></table>", target="search", old_content="old")
    assert res["status"] == "error"
    assert res["code"] == "TABLE_INSERT_FAILED"


@pytest.mark.parametrize("extra", [{}, {"all_matches": True}, {"occurrence": 0}])
def test_invalid_regex_is_invalid_regex_in_the_edit_path(extra):
    ctx = _icu_rejects(_edit_ctx())
    with patch("plugin.writer.html_import.content_has_markup", return_value=False), \
         patch("plugin.writer.search.find_literal_ranges") as find_lit:
        res = ApplyDocumentContent().execute(
            ctx, content="x", target="search", old_content="([a-", regex=True, **extra)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"
    assert res.get("count", res.get("details", {}).get("count")) == 0
    find_lit.assert_not_called()


def test_apply_document_content_selection_failure_not_silent():
    from plugin.framework.config import save
    save({'doc.agent_edit_review_mode': 'none'})
    from plugin.writer import html_import

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.apply_document_content.request_insert_range", return_value=(MagicMock(), False)), \
         patch.object(html_import, "insert_content_at_position",
                      side_effect=ToolExecutionError("Could not resolve the current selection")):
        # _execute maps ToolExecutionError to the standard tool error dict so chat/MCP
        # see status=error.
        result = ApplyDocumentContent().execute(ctx, content="x", target="selection")
    assert result["status"] == "error"
    assert "selection" in result.get("message", "")


def test_selection_snapped_insert_reports_snapped_to_paragraph():
    from plugin.framework.config import save
    save({"doc.agent_edit_review_mode": "none"})
    from plugin.writer import html_import
    from plugin.writer.html_import_styles import WriteReport

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.services.get.return_value = MagicMock()
    with (
        patch("plugin.writer.apply_document_content.request_insert_range", return_value=(None, False)),
        patch.object(
            html_import,
            "insert_content_at_position",
            return_value=WriteReport(snapped_to_paragraph=True),
        ),
    ):
        res_snapped = ApplyDocumentContent().execute(ctx, content="<p>A</p>", target="selection")
    assert res_snapped["status"] == "ok"
    assert res_snapped["snapped_to_paragraph"] is True

    with (
        patch("plugin.writer.apply_document_content.request_insert_range", return_value=(None, False)),
        patch.object(
            html_import,
            "insert_content_at_position",
            return_value=WriteReport(),
        ),
    ):
        res_plain = ApplyDocumentContent().execute(ctx, content="x", target="selection")
    assert res_plain["status"] == "ok"
    assert "snapped_to_paragraph" not in res_plain


def test_edit_selection_passes_pinned_range_to_html_import():
    from plugin.framework.tool import SelectionPin
    from plugin.writer import html_import

    ctx = _edit_ctx()
    pinned_rng = MagicMock()
    pinned_anchor = MagicMock()
    pinned_rng.getText.return_value.createTextCursorByRange.return_value = pinned_anchor
    ctx.selection_pin = SelectionPin(pinned_rng, ctx.doc)

    with patch.object(
        html_import,
        "insert_content_at_position",
        return_value=html_import_styles.WriteReport(),
    ) as mock_insert, patch(
        "plugin.writer.apply_document_content.attach_edited_context",
        side_effect=lambda res, _a: res,
    ) as mock_attach:
        res = ApplyDocumentContent().execute(ctx, content="new", target="selection")

    assert res["status"] == "ok"
    mock_insert.assert_called_once_with(ctx.doc, ctx.ctx, "new", "selection", selection=pinned_rng)
    mock_attach.assert_called_once()
    assert mock_attach.call_args[0][1] is pinned_anchor


def test_one_string_and_list_content_reach_import_the_same() -> None:
    from plugin.writer import html_import

    ctx = _edit_ctx()
    with patch.object(
        html_import,
        "insert_content_at_position",
        return_value=html_import_styles.WriteReport(),
    ) as mock_insert, patch(
        "plugin.writer.apply_document_content.attach_edited_context",
        side_effect=lambda res, _a: res,
    ):
        res_list = ApplyDocumentContent().execute(ctx, content=["<h1>A</h1>", "<p>B</p>"], target="end")
        res_str = ApplyDocumentContent().execute(ctx, content="<h1>A</h1>\n<p>B</p>", target="end")

    assert res_list["status"] == "ok"
    assert res_str["status"] == "ok"
    assert mock_insert.call_count == 2
    assert mock_insert.call_args_list[0] == mock_insert.call_args_list[1]
    assert mock_insert.call_args_list[0].args[2] == "<h1>A</h1>\n<p>B</p>"


def test_all_matches_table_insert_unchanged_count_returns_table_insert_failed() -> None:
    from types import SimpleNamespace

    ctx = _edit_ctx()
    tables = MagicMock()
    tables.getCount.return_value = 0
    ctx.doc.getTextTables.return_value = tables

    found = MagicMock()
    found.getString.return_value = "HIT"
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.apply_document_content.writer_tables_emptied_by_matches", return_value=[]), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=MagicMock()), \
         patch.object(
             ApplyDocumentContent,
             "_replace_found",
             side_effect=ToolExecutionError("Table insert produced no table in document.", code="TABLE_INSERT_FAILED"),
         ), \
         patch("plugin.writer.apply_document_content.commit_or_rollback") as mock_cor:
        def run_mutate(_doc, _title, mutate, session=None):
            try:
                mutate()
                return SimpleNamespace(
                    mutate_error=None, rolled_back=False, nothing_landed=False, rollback_error=None
                )
            except Exception as exc:
                return SimpleNamespace(
                    mutate_error=exc, rolled_back=True, nothing_landed=True, rollback_error=None
                )

        mock_cor.side_effect = run_mutate
        res = ApplyDocumentContent().execute(
            ctx,
            target="search",
            old_content="HIT",
            content="<table><tr><td>x</td></tr></table>",
            all_matches=True,
        )

    assert res["status"] == "error"
    assert res["code"] == "TABLE_INSERT_FAILED"
    assert res["partial_failure"] is True


def test_dry_run_rejects_non_string_content_list_items() -> None:
    res = ApplyDocumentContent().execute(
        _edit_ctx(),
        target="search",
        old_content="HIT",
        content=[None],
        dry_run=True,
    )
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "content list items must be strings."


@pytest.mark.parametrize("mode", ["off", "record"])
@pytest.mark.parametrize("left", [True, False])
def test_record_mode_annotates_only_when_session_left_changes(mode: str, left: bool) -> None:
    ctx = _edit_ctx()
    session = MagicMock()
    session.left_tracked_changes.return_value = left
    with (
        patch("plugin.writer.apply_document_content.main.is_current", return_value=True),
        patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value=mode),
        patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode),
        patch("plugin.writer.apply_document_content.EditReviewSession", return_value=session),
        patch.object(
            ApplyDocumentContent,
            "_dispatch_edit",
            return_value={"status": "ok", "message": "Replaced 1 occurrence."},
        ),
    ):
        res = ApplyDocumentContent().execute(ctx, target="search", old_content="A", content="B")

    assert res["status"] == "ok"
    assert ("pending_review" in res) is left
    assert res.get("tracked") is (True if left else None)
    assert ("review_mode" in res) is (left and mode == "record")


def test_wait_mode_without_registered_change_reports_untagged_pending() -> None:
    ctx = _edit_ctx()
    session = MagicMock()
    session.changes = []
    session.left_tracked_changes.return_value = True
    with (
        patch("plugin.writer.apply_document_content.main.is_current", return_value=False),
        patch("plugin.writer.apply_document_content._run_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="wait"),
        patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value="wait"),
        patch("plugin.writer.apply_document_content.EditReviewSession", return_value=session),
        patch.object(
            ApplyDocumentContent,
            "_dispatch_edit",
            return_value={"status": "ok", "message": "Replaced 1 occurrence."},
        ),
    ):
        res = ApplyDocumentContent().execute(ctx, target="search", old_content="A", content="")

    assert res["status"] == "ok"
    assert res["pending_review"] is True
    assert res["review_mode"] == "wait"
    session.wait_for_review.assert_not_called()


def test_replacement_plain_preserves_tex_escapes() -> None:
    assert ApplyDocumentContent._replacement_plain(r"$\nu$") == r"$\nu$"
    assert ApplyDocumentContent._replacement_plain(r"Line\nTwo") == "Line\nTwo"


@pytest.mark.parametrize(
    ("old_content", "content", "matched", "expected_url"),
    [
        ("Old title", "Old &amp; new", "Old title", "#1.Old & new|outline"),
        (" title", " New", "title", "#1.Old New|outline"),
    ],
)
def test_dry_run_outline_preview_uses_edit_replacement_text(
    old_content: str, content: str, matched: str, expected_url: str,
) -> None:
    from plugin.writer.hyperlink_fixup import OutlineLink, OutlineSnapshot

    ctx = _edit_ctx()
    found = MagicMock()
    snapshot = OutlineSnapshot(
        links=[OutlineLink(url="#1.Old title|outline", name="1.Old title", target="")],
        prefix="Old " if matched == "title" else "",
        suffix="",
        matched=matched,
        single_paragraph=True,
    )
    with patch("plugin.writer.search.find_literal_ranges", return_value=([found], MatchMode(True, False))), \
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.apply_document_content.capture_outline_hyperlinks", return_value=snapshot):
        res = ApplyDocumentContent().execute(
            ctx,
            content=content,
            target="search",
            old_content=old_content,
            dry_run=True,
        )
    assert res["status"] == "ok", res
    assert res["replaceable"][0]["hyperlinks"][0]["hyperlink_url_after"] == expected_url


def test_dry_run_refuses_what_the_edit_refuses() -> None:
    kwargs: dict[str, Any] = {
        "target": "search",
        "old_content": "x",
        "content": "y",
        "position": "before",
        "all_matches": True,
    }
    res = ApplyDocumentContent().execute(_edit_ctx(), dry_run=True, **kwargs)
    expected = ApplyDocumentContent().execute(_edit_ctx(), **kwargs)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["code"] == expected["code"]
    assert res["message"] == expected["message"]
