# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from plugin.doc.text_helpers import normalize_linebreaks as _normalize
from plugin.writer.format import (
    document_to_content,
    insert_content_at_position as _insert_content_at_position,
    content_has_markup as _content_has_markup,
    replace_preserving_format as _replace_text_preserving_format,
    find_text_ranges,
)
from plugin.framework.uno_context import get_desktop

def _move_cursor_by_offset(cursor, offset, expand=False):
    """Move cursor by offset in chunks to handle UNO's short (16-bit) limitation."""
    remaining = offset
    while remaining > 0:
        n = min(remaining, 8192)
        cursor.goRight(n, expand)
        remaining -= n


def _tool_ctx(doc, ctx):
    """Build ToolContext for writer tools (uses real get_services())."""
    from plugin.main import get_services
    from plugin.framework.tool import ToolContext
    return ToolContext(doc, ctx, "writer", get_services(), "test")


def _get_document_content(doc, ctx, params):
    """Call real get_document_content tool; returns dict."""
    from plugin.main import get_tools
    scope = params.get("scope", "full")
    kwargs = {"scope": scope}
    if params.get("max_chars") is not None:
        kwargs["max_chars"] = params["max_chars"]
    if scope == "range":
        if params.get("start") is not None:
            kwargs["start"] = params["start"]
        if params.get("end") is not None:
            kwargs["end"] = params["end"]
    try:
        return get_tools().execute("get_document_content", _tool_ctx(doc, ctx), **kwargs)
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _apply_document_content(doc, ctx, params):
    """Call real apply_document_content tool; returns dict."""
    # GHA 34683742049: leftover Hidden _default apply hung after the
    # first apply-suite skips. Later apply UNO files skip the same load.
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    from plugin.main import get_tools
    content = params.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    kwargs = {"content": content}
    if params.get("target") is not None:
        kwargs["target"] = params["target"]
    if params.get("old_content") is not None:
        kwargs["old_content"] = params["old_content"]
    if params.get("all_matches") is not None:
        kwargs["all_matches"] = params["all_matches"]
    try:
        return get_tools().execute("apply_document_content", _tool_ctx(doc, ctx), **kwargs)
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _find_text(doc, ctx, params):
    """Call find_text_ranges and return same dict shape as former _find_text."""
    search = params.get("search", "")
    case_sensitive = params.get("case_sensitive", True)
    try:
        ranges = find_text_ranges(doc, ctx, search, case_sensitive=case_sensitive)
        return {"status": "ok", "ranges": ranges}
    except Exception as e:
        return {"status": "error", "message": str(e)}


from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    skip_windows_leftover_hidden_load,
    skip_windows_pooled_writer_reuse,
    with_native_doc,
)


def _read_doc_text(d):
    raw = d.getText().createTextCursor()
    raw.gotoStart(False)
    raw.gotoEnd(True)
    return raw.getString()


@native_test
@with_native_doc("writer")
def test_document_to_content(ctx, doc):
    md = document_to_content(doc, ctx, None, scope="full")
    assert isinstance(md, str), f"document_to_content did not return string: {type(md)}"


@native_test
@with_native_doc("writer")
def test_tool_get_document_content(ctx, doc):
    result = _get_document_content(doc, ctx, {"scope": "full"})
    assert result.get("status") == "ok", f"tool_get_document_content failed: {result}"
    assert "content" in result, "Missing content"


@native_test
@with_native_doc("writer")
def test_get_document_content_returns_document_length(ctx, doc):
    result = _get_document_content(doc, ctx, {"scope": "full"})
    doc_len_actual = len(_read_doc_text(doc))
    assert result.get("status") == "ok", f"tool_get_document_content failed: {result}"
    assert result.get("document_length") == doc_len_actual, f"Length mismatch: {result.get('document_length')} vs {doc_len_actual}"


@native_test
@with_native_doc("writer")
def test_apply_at_end_via_insert_content(ctx, doc):
    test_content = "Format test\n\nThis was inserted by the test."
    insert_needle = "Format test"

    _insert_content_at_position(doc, ctx, test_content, "end")
    full_text = _read_doc_text(doc)
    assert insert_needle in full_text, "Content not found after apply at end"


@native_test
@with_native_doc("writer")
def test_search_and_replace(ctx, doc):
    marker = "REPLACE_ME_MARKER"
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "\n" + marker, False)

    replacement = "<b>replaced</b>"

    result = _apply_document_content(doc, ctx, {
        "content": replacement,
        "old_content": marker,
    })
    full_text = _read_doc_text(doc)
    assert result.get("status") == "ok", f"search-and-replace failed: {result}"
    assert "replaced" in full_text, "'replaced' not found"
    assert marker not in full_text, "marker not gone"


@native_test
@with_native_doc("writer")
def test_get_document_content_scope_range(ctx, doc):
    full_text = _read_doc_text(doc)
    if len(full_text) >= 10:
        result = _get_document_content(doc, ctx, {"scope": "range", "start": 0, "end": 10})
        assert result.get("status") == "ok", f"get_document_content scope=range failed: {result}"
        assert result.get("start") == 0 and result.get("end") == 10 and "content" in result, "Malformed response"


@native_test
@with_native_doc("writer")
def test_find_text(ctx, doc):
    marker_find = "FIND_ME_UNIQUE_xyz"
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "\n" + marker_find, False)

    result = _find_text(doc, ctx, {
        "search": marker_find,
        "case_sensitive": True
    })
    assert result.get("status") == "ok", f"find_text failed: {result}"
    ranges = result.get("ranges", [])
    assert len(ranges) == 1, f"Expected 1 match, got {len(ranges)}"
    r = ranges[0]
    text_at_range = _read_doc_text(doc)[r["start"]:r["end"]]
    assert text_at_range == marker_find, f"find_text mismatch. Expected '{marker_find}', got '{text_at_range}'"


@native_test
@with_native_doc("writer")
def test_html_linebreak_preservation(ctx, doc):
    """Plain newlines survive apply_document_content(target='full_document').

    Helper coverage of the HTML conversion lives in tests/doc/test_linebreak.py,
    but that never writes a Writer document. This checks the apply round-trip.
    """
    plain_input = "Line 1\nLine 2\n\nParagraph 2"
    result = _apply_document_content(doc, ctx, {
        "content": plain_input,
        "target": "full_document",
    })
    assert result.get("status") == "ok", f"linebreak apply failed: {result}"
    full_text = _read_doc_text(doc)
    assert "Line 1" in full_text and "Line 2" in full_text and "Paragraph 2" in full_text, (
        f"HTML linebreak preservation failed: {full_text!r}"
    )


@native_test
@with_native_doc("writer")
def test_crlf_normalization(ctx, doc):
    """CRLF in applied content is stored as LF and find_text offsets agree.

    normalize_linebreaks itself is covered in tests/doc/test_text_helpers.py.
    This checks the apply + find_text path, which that unit test does not.
    """
    crlf_input = "Line A\r\nLine B"
    marker_u = "UNIQUE_CRLF_TEST"
    payload = crlf_input + "\n" + marker_u

    result = _apply_document_content(doc, ctx, {
        "content": payload,
        "target": "full_document",
    })
    assert result.get("status") == "ok", f"crlf apply failed: {result}"

    res_find = _find_text(doc, ctx, {"search": marker_u})
    assert res_find.get("status") == "ok" and res_find.get("ranges"), "Could not find test payload"
    r = res_find["ranges"][0]

    res_find_start = _find_text(doc, ctx, {"search": "Line A"})
    assert res_find_start.get("status") == "ok" and res_find_start.get("ranges"), "Could not find 'Line A' for verification"
    r_start = res_find_start["ranges"][-1]

    total_range_text = _read_doc_text(doc)[r_start["start"]:r["end"]]
    expected_norm = "Line A\nLine B\nUNIQUE_CRLF_TEST"
    assert total_range_text == expected_norm, f"Expected {repr(expected_norm)}, got {repr(total_range_text)}"


@native_test
def test_content_has_markup_auto_detection():
    assert _content_has_markup("**bold**")
    assert _content_has_markup("<b>bold</b>")
    assert _content_has_markup("<ruby>漢字<rt>かんじ</rt></ruby>")
    assert _content_has_markup("# Heading")
    assert _content_has_markup("| col1 | col2 |")
    assert not _content_has_markup("Jane Doe")
    assert not _content_has_markup("Hello world, this is plain text.")
    assert not _content_has_markup("")
    assert not _content_has_markup(None)


# Helper: create text with per-character background colors and return the range
COLORS = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF]  # Red Green Blue Yellow Magenta

def _create_colored_text(doc, chars):
    """Insert chars at end of doc, color each char, return an XTextCursor spanning them."""
    text = doc.getText()
    sep_cursor = text.createTextCursor()
    sep_cursor.gotoEnd(False)
    text.insertString(sep_cursor, "\n", False)

    def get_accurate_offset():
        c = text.createTextCursor()
        c.gotoStart(False)
        c.gotoEnd(True)
        return len(_normalize(c.getString()))

    start_off = get_accurate_offset()

    for i, ch in enumerate(chars):
        ins = text.createTextCursor()
        ins.gotoEnd(False)
        text.insertString(ins, ch, False)
        cc = text.createTextCursor()
        cc.gotoEnd(False)
        cc.goLeft(1, True)
        cc.setPropertyValue("CharBackColor", COLORS[i % len(COLORS)])

    range_cursor = text.createTextCursor()
    range_cursor.gotoStart(False)
    _move_cursor_by_offset(range_cursor, start_off)
    _move_cursor_by_offset(range_cursor, len(chars), expand=True)
    return range_cursor


def _get_char_colors(doc, range_cursor):
    """Read CharBackColor for each character in the range. Returns list of ints."""
    text = doc.getText()
    colors = []
    pos = text.createTextCursorByRange(range_cursor.getStart())
    range_text = range_cursor.getString()
    for i in range(len(range_text)):
        cc = text.createTextCursorByRange(pos)
        cc.goRight(1, True)
        try:
            colors.append(cc.getPropertyValue("CharBackColor"))
        except Exception:
            colors.append(-1)
        pos = text.createTextCursorByRange(cc.getEnd())
    return colors


def _insert_colored_chars_at_end(doc, chars, *, color_offset=0):
    """Insert *chars* at document end with per-char CharBackColor."""
    text = doc.getText()
    for i, ch in enumerate(chars):
        ins = text.createTextCursor()
        ins.gotoEnd(False)
        text.insertString(ins, ch, False)
        cc = text.createTextCursor()
        cc.gotoEnd(False)
        cc.goLeft(1, True)
        cc.setPropertyValue("CharBackColor", COLORS[(color_offset + i) % len(COLORS)])


def _cursor_spanning_offsets(doc, start_off, length):
    """Return an XTextCursor selecting *length* normalized chars from *start_off*."""
    text = doc.getText()
    range_cursor = text.createTextCursor()
    range_cursor.gotoStart(False)
    _move_cursor_by_offset(range_cursor, start_off)
    _move_cursor_by_offset(range_cursor, length, expand=True)
    return range_cursor


def _paragraph_strings(doc):
    """Body paragraph texts in document order (normalized)."""
    text = doc.getText()
    paras = []
    enum = text.createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if not (hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph")):
            continue
        cur = text.createTextCursorByRange(el.getStart())
        cur.gotoRange(el.getEnd(), True)
        paras.append(_normalize(cur.getString()))
    return paras


def _letter_colors_from_range(doc, range_cursor):
    """CharBackColor for non-linebreak characters in *range_cursor*.

    Windows ``getString()`` can emit ``\\r`` or ``\\r\\n`` for a paragraph
    break while Linux emits ``\\n``. Filtering only ``\\n`` counted the
    CR as a letter (GHA 35466498641).
    """
    return [
        c
        for ch, c in zip(range_cursor.getString(), _get_char_colors(doc, range_cursor))
        if ch not in "\r\n"
    ]


@native_test
@with_native_doc("writer")
def test_cross_paragraph_same_length_replacement_preserves_colors(ctx, doc):
    """replace_preserving_format across a paragraph break keeps per-char background colors."""
    # GHA 34685648395: leftover_open>0 reuse failed a bare color assert
    # (empty AssertionError). Same leftover-pollution class as the
    # apply-style origin canary — not a product color regression.
    skip_windows_leftover_hidden_load("format_uno cross-paragraph color leftover reuse")
    # GHA 35470191616 (master c4fdbee, #809): leftover_open=0 after
    # impress recycle still printed native_doc leftover writer reuse.
    # skip_windows_leftover_hidden_load did not fire. Sibling
    # test_same_length_replacement_preserves_colors passed on that
    # reuse path — leftover body/findFirst pollution, not format.py.
    # #809's \r\n color filter was not enough. Do not factory-load a
    # second Hidden _blank (34652644656 hung 30s at leftover_open=0).
    skip_windows_pooled_writer_reuse("format_uno cross-paragraph color pooled reuse")
    text = doc.getText()
    sep = text.createTextCursor()
    sep.gotoEnd(False)
    text.insertString(sep, "\n", False)

    def body_len():
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        return len(_normalize(cur.getString()))

    start_off = body_len()
    para1 = "HELLO"
    para2 = "WORLD"
    _insert_colored_chars_at_end(doc, para1, color_offset=0)
    br = text.createTextCursor()
    br.gotoEnd(False)
    text.insertControlCharacter(br, 0, False)  # PARAGRAPH_BREAK
    _insert_colored_chars_at_end(doc, para2, color_offset=len(para1))
    end_off = body_len()
    span_len = end_off - start_off

    rng = _cursor_spanning_offsets(doc, start_off, span_len)
    old_text = _normalize(rng.getString())
    assert old_text == "HELLO\nWORLD", f"setup range text: {old_text!r}"

    expected_para1_colors = [COLORS[i % len(COLORS)] for i in range(len(para1))]
    expected_para2_colors = [COLORS[(len(para1) + i) % len(COLORS)] for i in range(len(para2))]
    actual_setup = _letter_colors_from_range(doc, rng)
    assert actual_setup == expected_para1_colors + expected_para2_colors, (
        f"setup colors: expected {expected_para1_colors + expected_para2_colors} got {actual_setup}"
    )

    new_text = "YELLO\nWORLD"
    assert len(new_text) == len(old_text)
    _replace_text_preserving_format(doc, rng, new_text, ctx)

    paras = _paragraph_strings(doc)
    yello_paras = [p for p in paras if "YELLO" in p]
    world_paras = [p for p in paras if "WORLD" in p]
    assert len(yello_paras) == 1 and len(world_paras) == 1, paras
    assert yello_paras[0] != world_paras[0], "paragraphs merged after cross-paragraph replace"

    sd = doc.createSearchDescriptor()
    sd.SearchString = "YELLO"
    found_yello = doc.findFirst(sd)
    assert found_yello, "YELLO not found after replace"
    sd.SearchString = "WORLD"
    found_world = doc.findFirst(sd)
    assert found_world, "WORLD not found after replace"

    actual_yello = _letter_colors_from_range(doc, found_yello)
    assert actual_yello == expected_para1_colors, (
        f"YELLO colors: expected {expected_para1_colors} got {actual_yello}"
    )
    actual_world = _letter_colors_from_range(doc, found_world)
    assert actual_world == expected_para2_colors, (
        f"WORLD colors: expected {expected_para2_colors} got {actual_world}"
    )


@native_test
@with_native_doc("writer")
def test_same_length_replacement_preserves_colors(ctx, doc):
    old_chars = "ABCDE"
    rng = _create_colored_text(doc, old_chars)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(old_chars))]

    actual_before = _get_char_colors(doc, rng)
    assert actual_before == expected_colors, f"SETUP FAILED: expected {expected_colors} got {actual_before}"

    _replace_text_preserving_format(doc, rng, "PQRST", ctx)
    sd = doc.createSearchDescriptor()
    sd.SearchString = "PQRST"
    found = doc.findFirst(sd)
    assert found, "'PQRST' not found after replace"
    actual_colors = _get_char_colors(doc, found)
    assert actual_colors == expected_colors and found.getString() == "PQRST", f"Expected {expected_colors}, got {actual_colors}, text='{found.getString()}'"


@native_test
@with_native_doc("writer")
def test_longer_replacement_inherits_last_color(ctx, doc):
    old_chars = "ABC"
    rng = _create_colored_text(doc, old_chars)
    expected_setup = [COLORS[0], COLORS[1], COLORS[2]]
    actual_before = _get_char_colors(doc, rng)
    assert actual_before == expected_setup, f"SETUP FAILED: expected {expected_setup} got {actual_before}"

    expected_colors = [
        COLORS[0], COLORS[1], COLORS[2],  # overlap: inherit from A, B, C
        COLORS[2], COLORS[2],             # extra chars: inherit from last (C = Blue)
    ]
    _replace_text_preserving_format(doc, rng, "MNOPQ", ctx)
    sd = doc.createSearchDescriptor()
    sd.SearchString = "MNOPQ"
    found = doc.findFirst(sd)
    assert found, "'MNOPQ' not found after replace"
    actual_colors = _get_char_colors(doc, found)
    assert actual_colors == expected_colors and found.getString() == "MNOPQ", f"Expected {expected_colors}, got {actual_colors}, text='{found.getString()}'"


@native_test
@with_native_doc("writer")
def test_shorter_replacement_leftover_deleted(ctx, doc):
    old_chars = "ABCDE"
    rng = _create_colored_text(doc, old_chars)
    expected_setup = [COLORS[i % len(COLORS)] for i in range(5)]
    actual_before = _get_char_colors(doc, rng)
    assert actual_before == expected_setup, f"SETUP FAILED: expected {expected_setup} got {actual_before}"

    expected_colors = [COLORS[0], COLORS[1]]  # only first 2 colors survive
    _replace_text_preserving_format(doc, rng, "UV", ctx)
    sd = doc.createSearchDescriptor()
    sd.SearchString = "UV"
    found = doc.findFirst(sd)
    assert found, "'UV' not found after replace"
    actual_colors = _get_char_colors(doc, found)
    result_text = found.getString()
    assert actual_colors == expected_colors and result_text == "UV", f"Expected {expected_colors}, got {actual_colors}, text={repr(result_text)}"


@native_test
@with_native_doc("writer")
def test_long_replacement_process_events(ctx, doc):
    long_len = 50
    old_chars = "X" * long_len
    rng = _create_colored_text(doc, old_chars)

    new_chars = "Y" * long_len
    _replace_text_preserving_format(doc, rng, new_chars, ctx)

    sd = doc.createSearchDescriptor()
    sd.SearchString = new_chars
    found = doc.findFirst(sd)
    assert found and found.getString() == new_chars, "Failed to replace %d chars" % long_len


def _insert_table_with_cell_text(doc, cell_name, cell_text):
    """Insert a 2x2 table at doc end with *cell_text* in *cell_name* (e.g. 'A1')."""
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    ins = text.createTextCursor()
    ins.gotoEnd(False)
    text.insertTextContent(ins, tbl, False)
    tbl.getCellByName(cell_name).setString(cell_text)
    return tbl


@native_test
@with_native_doc("writer")
def test_replace_preserving_format_table_cell_uno(ctx, doc):
    """replace_preserving_format must use the cell's XText, not model.getText() body."""
    tbl = _insert_table_with_cell_text(doc, "A1", "TableCell")
    sd = doc.createSearchDescriptor()
    sd.SearchString = "TableCell"
    found = doc.findFirst(sd)
    assert found is not None, "setup: TableCell not found"
    _replace_text_preserving_format(doc, found, "TableCell-EDIT", ctx)
    assert "TableCell-EDIT" in tbl.getCellByName("A1").getString()


def _insert_colored_word(doc, word):
    """Insert word at end of doc with per-char background colors. Returns (start_offset, end_offset)."""
    text = doc.getText()
    sep = text.createTextCursor()
    sep.gotoEnd(False)
    text.insertString(sep, "\n", False)

    def get_accurate_offset():
        c = text.createTextCursor()
        c.gotoStart(False)
        c.gotoEnd(True)
        return len(_normalize(c.getString()))

    start_off = get_accurate_offset()
    for i, ch in enumerate(word):
        ins = text.createTextCursor()
        ins.gotoEnd(False)
        text.insertString(ins, ch, False)
        cc = text.createTextCursor()
        cc.gotoEnd(False)
        cc.goLeft(1, True)
        cc.setPropertyValue("CharBackColor", COLORS[i % len(COLORS)])
    end_off = get_accurate_offset()
    return start_off, end_off


def _get_colors_at_range(doc, start_off, length):
    """Read CharBackColor for `length` chars starting at absolute offset start_off."""
    text = doc.getText()
    colors = []
    pos_cursor = text.createTextCursor()
    pos_cursor.gotoStart(False)
    _move_cursor_by_offset(pos_cursor, start_off)
    for _ in range(length):
        cc = text.createTextCursorByRange(pos_cursor)
        cc.goRight(1, True)
        try:
            colors.append(cc.getPropertyValue("CharBackColor"))
        except Exception:
            colors.append(-1)
        pos_cursor = text.createTextCursorByRange(cc.getEnd())
    return colors


def _check_colors_at_search(doc, search_str, expected_colors):
    """Find search_str in doc and check its per-char colors."""
    text = doc.getText()
    sd = doc.createSearchDescriptor()
    sd.SearchString = search_str
    found = doc.findFirst(sd)
    if not found:
        return False, "not found in document"
    tmp = text.createTextCursorByRange(found.getStart())
    tmp.gotoStart(True)
    start_off = len(_normalize(tmp.getString()))
    actual = _get_colors_at_range(doc, start_off, len(search_str))
    if actual == expected_colors:
        return True, ""
    return False, "expected %s got %s" % (expected_colors, actual)


@native_test
@with_native_doc("writer")
def test_apply_document_content_target_search_preserves_colors(ctx, doc):
    word = "zBertPicklez"   # unique sentinel around the name
    start_off, end_off = _insert_colored_word(doc, word)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

    result = _apply_document_content(doc, ctx, {
        "content": "zBertTicklez",
        "old_content": "zBertPicklez",
    })
    assert result.get("status") == "ok", f"tool target=search failed: {result}"
    ok_flag, detail = _check_colors_at_search(doc, "zBertTicklez", expected_colors)
    assert ok_flag, f"colors not preserved: {detail}"


@native_test
@with_native_doc("writer")
def test_apply_document_content_target_range_preserves_colors(ctx, doc):
    word = "zNormaFlintez"
    start_off, end_off = _insert_colored_word(doc, word)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

    result = _apply_document_content(doc, ctx, {
        "content": "zNormaGlintez",
        "old_content": "zNormaFlintez",
    })
    assert result.get("status") == "ok", f"tool target=range failed: {result}"
    ok_flag, detail = _check_colors_at_search(doc, "zNormaGlintez", expected_colors)
    assert ok_flag, f"colors not preserved: {detail}"


@native_test
@with_native_doc("writer")
def test_apply_document_content_target_full_preserves_colors(ctx, doc):
    # GHA 34685648395: apply skip inside _apply_document_content fired,
    # then finally small_doc.close(True) hung 30s. Leftover Hidden
    # `_blank` + raw close (same family as document-scripts reopen).
    # Skip before the factory load so finally never runs.
    skip_windows_leftover_hidden_load("format_uno Hidden _blank small_doc")
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    small_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert small_doc and hasattr(small_doc, "getText"), "Could not create small doc"

    try:
        small_text = small_doc.getText()
        word = "zGordonCrumpz"
        new_word = "zGordonStumpz"
        for i, ch in enumerate(word):
            ins = small_text.createTextCursor()
            ins.gotoEnd(False)
            small_text.insertString(ins, ch, False)
            cc = small_text.createTextCursor()
            cc.gotoEnd(False)
            cc.goLeft(1, True)
            cc.setPropertyValue("CharBackColor", COLORS[i % len(COLORS)])
        expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

        result = _apply_document_content(small_doc, ctx, {
            "content": new_word,
            "old_content": word,
        })
        assert result.get("status") == "ok", f"tool target=full failed: {result}"

        sd = small_doc.createSearchDescriptor()
        sd.SearchString = new_word
        found = small_doc.findFirst(sd)
        assert found, f"'{new_word}' not found after replace"

        small_txt = small_doc.getText()
        tmp = small_txt.createTextCursorByRange(found.getStart())
        tmp.gotoStart(True)
        start_off2 = len(_normalize(tmp.getString()))
        actual_colors = []
        pos_c = small_txt.createTextCursor()
        pos_c.gotoStart(False)
        _move_cursor_by_offset(pos_c, start_off2)
        for _ in range(len(new_word)):
            cc2 = small_txt.createTextCursorByRange(pos_c)
            cc2.goRight(1, True)
            try:
                actual_colors.append(cc2.getPropertyValue("CharBackColor"))
            except Exception:
                actual_colors.append(-1)
            pos_c = small_txt.createTextCursorByRange(cc2.getEnd())

        assert actual_colors == expected_colors, f"colors expected {expected_colors} got {actual_colors}"
    finally:
        try:
            small_doc.close(True)
        except Exception:
            pass


# Frozen post-process fixture (same shape as prepare_html_for_lo_import output).
_VISION_HTML_FIXTURE = (
    '<h2 style="font-size: 14pt; font-weight: bold;color: #333;">SECTION HEADING</h2>'
    '<p style="font-family: Arial, sans-serif; line-height: 1.6;">Body paragraph text.</p>'
)


def _is_bold_char_weight(wv) -> bool:
    if wv is None:
        return False
    try:
        from com.sun.star.awt import FontWeight

        if wv == FontWeight.BOLD:
            return True
    except Exception:
        pass
    try:
        return float(wv) >= 135.0
    except (TypeError, ValueError):
        return False


def _first_char_props_at_search(doc, needle: str) -> tuple[float | None, float | None]:
    sd = doc.createSearchDescriptor()
    sd.SearchString = needle
    found = doc.findFirst(sd)
    if found is None:
        return None, None
    text_obj = found.getText()
    cursor = text_obj.createTextCursorByRange(found.getStart())
    try:
        cursor.goRight(1, True)
        weight = cursor.getPropertyValue("CharWeight")
        height = cursor.getPropertyValue("CharHeight")
        return float(weight), float(height)
    except Exception:
        return None, None


@native_test
@with_native_doc("writer")
def test_vision_html_insert_heading_bolder_and_larger_than_body(ctx, doc):
    """UNO: vision OCR HTML insert preserves heading weight/size in Writer."""
    insert_content_at_position = _insert_content_at_position
    insert_content_at_position(doc, ctx, _VISION_HTML_FIXTURE, "end")
    full_text = doc.getText().getString()
    assert "SECTION HEADING" in full_text, f"heading text missing: {full_text!r}"
    assert "Body paragraph" in full_text, f"body text missing: {full_text!r}"

    heading_weight, heading_height = _first_char_props_at_search(doc, "SECTION HEADING")
    body_weight, body_height = _first_char_props_at_search(doc, "Body paragraph")

    assert heading_weight is not None, "heading paragraph not found after HTML import"
    assert body_weight is not None, "body paragraph not found after HTML import"
    assert _is_bold_char_weight(heading_weight), f"expected bold heading, CharWeight={heading_weight!r}"
    assert not _is_bold_char_weight(body_weight), f"expected normal body weight, CharWeight={body_weight!r}"
    assert heading_height > body_height, (
        f"expected heading CharHeight ({heading_height}) > body ({body_height})"
    )



