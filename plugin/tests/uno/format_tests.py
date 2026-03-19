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
from plugin.modules.writer.format_support import (
    document_to_content,
    insert_content_at_position as _insert_content_at_position,
    content_has_markup as _content_has_markup,
    replace_preserving_format as _replace_text_preserving_format,
    find_text_ranges,
    _normalize,
)
from plugin.framework.logging import debug_log
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
    from plugin.framework.tool_context import ToolContext
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
    """Call real apply_document_content tool; returns dict. Requires content and old_content."""
    from plugin.main import get_tools
    content = params.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    kwargs = {"content": content, "old_content": params.get("old_content", "")}
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


from plugin.testing_runner import setup, teardown, native_test


_test_doc = None
_test_ctx = None


@setup
def setup_format_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True

    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Writer document"
    assert hasattr(_test_doc, "getText"), "Not a valid Writer document"

    debug_log("format_tests: run start", context="Tests")


@teardown
def teardown_format_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _read_doc_text(d):
    raw = d.getText().createTextCursor()
    raw.gotoStart(False)
    raw.gotoEnd(True)
    return raw.getString()


@native_test
def test_document_to_content():
    md = document_to_content(_test_doc, _test_ctx, None, scope="full")
    assert isinstance(md, str), f"document_to_content did not return string: {type(md)}"


@native_test
def test_tool_get_document_content():
    result = _get_document_content(_test_doc, _test_ctx, {"scope": "full"})
    assert result.get("status") == "ok", f"tool_get_document_content failed: {result}"
    assert "content" in result, "Missing content"


@native_test
def test_get_document_content_returns_document_length():
    result = _get_document_content(_test_doc, _test_ctx, {"scope": "full"})
    doc_len_actual = len(_read_doc_text(_test_doc))
    assert result.get("status") == "ok", f"tool_get_document_content failed: {result}"
    assert result.get("document_length") == doc_len_actual, f"Length mismatch: {result.get('document_length')} vs {doc_len_actual}"


@native_test
def test_apply_at_end_via_insert_content():
    test_content = "Format test\n\nThis was inserted by the test."
    insert_needle = "Format test"

    _insert_content_at_position(_test_doc, _test_ctx, test_content, "end")
    full_text = _read_doc_text(_test_doc)
    assert insert_needle in full_text, "Content not found after apply at end"


@native_test
def test_apply_document_content_target_end():
    test_content = "Format test\n\nThis was inserted by the test."
    insert_needle = "Format test"
    full_doc = _read_doc_text(_test_doc)
    new_content = (full_doc + "\n" + test_content) if full_doc else test_content

    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": new_content,
        "old_content": full_doc if full_doc else "",
    })
    assert result.get("status") == "ok", f"_apply_document_content failed: {result}"
    full_text = _read_doc_text(_test_doc)
    assert insert_needle in full_text, "Content not found after _apply_document_content"


@native_test
def test_formatted_content():
    formatted_input = "<h1>Heading</h1><p><b>Bold text</b> and <i>italic text</i></p>"
    full_doc = _read_doc_text(_test_doc)
    new_content = (full_doc + "\n" + formatted_input) if full_doc else formatted_input
    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": new_content,
        "old_content": full_doc if full_doc else "",
    })
    assert result.get("status") == "ok", f"formatted content failed: {result}"

    full_text = _read_doc_text(_test_doc)
    has_heading = "Heading" in full_text
    has_bold = "Bold" in full_text
    has_italic = "italic" in full_text
    assert has_heading or has_bold or has_italic, "Formatting keywords not found"


@native_test
def test_search_and_replace():
    marker = "REPLACE_ME_MARKER"
    text = _test_doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "\n" + marker, False)

    replacement = "<b>replaced</b>"

    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": replacement,
        "old_content": marker,
    })
    full_text = _read_doc_text(_test_doc)
    assert result.get("status") == "ok", f"search-and-replace failed: {result}"
    assert "replaced" in full_text, "'replaced' not found"
    assert marker not in full_text, "marker not gone"


@native_test
def test_list_input_accommodation():
    list_input = ["item_a", "item_b"]
    full_doc = _read_doc_text(_test_doc)
    new_content = (full_doc + "\nitem_a\nitem_b") if full_doc else "item_a\nitem_b"
    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": new_content,
        "old_content": full_doc if full_doc else "",
    })
    full_text = _read_doc_text(_test_doc)
    assert result.get("status") == "ok", f"list input failed: {result}"
    assert "item_a" in full_text and "item_b" in full_text, "list input content missing"


@native_test
def test_target_full():
    full_replacement = "<h1>Full Replace Test</h1><p>Only this content should remain.</p>"
    full_doc = _read_doc_text(_test_doc)
    result = _apply_document_content(_test_doc, _test_ctx, {"content": full_replacement, "old_content": full_doc})
    full_text = _read_doc_text(_test_doc)
    assert result.get("status") == "ok", f"target=full failed: {result}"
    assert "Full Replace" in full_text, "'Full Replace' not found"


@native_test
def test_target_range():
    full_doc = _read_doc_text(_test_doc)
    range_content = "<h2>Range Replace</h2><p>Replaced [0, %d).</p>" % len(full_doc)
    result = _apply_document_content(_test_doc, _test_ctx, {"content": range_content, "old_content": full_doc})
    full_text = _read_doc_text(_test_doc)
    assert result.get("status") == "ok", f"target=range failed: {result}"
    assert "Range Replace" in full_text, "'Range Replace' not found"


@native_test
def test_get_document_content_scope_range():
    full_text = _read_doc_text(_test_doc)
    if len(full_text) >= 10:
        result = _get_document_content(_test_doc, _test_ctx, {"scope": "range", "start": 0, "end": 10})
        assert result.get("status") == "ok", f"get_document_content scope=range failed: {result}"
        assert result.get("start") == 0 and result.get("end") == 10 and "content" in result, "Malformed response"


@native_test
def test_find_text():
    marker_find = "FIND_ME_UNIQUE_xyz"
    text = _test_doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "\n" + marker_find, False)

    result = _find_text(_test_doc, _test_ctx, {
        "search": marker_find,
        "case_sensitive": True
    })
    assert result.get("status") == "ok", f"find_text failed: {result}"
    ranges = result.get("ranges", [])
    assert len(ranges) == 1, f"Expected 1 match, got {len(ranges)}"
    r = ranges[0]
    text_at_range = _read_doc_text(_test_doc)[r["start"]:r["end"]]
    assert text_at_range == marker_find, f"find_text mismatch. Expected '{marker_find}', got '{text_at_range}'"


@native_test
def test_html_linebreak_preservation():
    plain_input = "Line 1\nLine 2\n\nParagraph 2"
    full_doc = _read_doc_text(_test_doc)
    new_content = (full_doc + "\n" + plain_input) if full_doc else plain_input
    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": new_content,
        "old_content": full_doc if full_doc else "",
    })
    full_text = _read_doc_text(_test_doc)
    assert "Line 1" in full_text and "Line 2" in full_text and "Paragraph 2" in full_text, "HTML linebreak preservation failed"


@native_test
def test_crlf_normalization():
    crlf_input = "Line A\r\nLine B"
    marker_u = "UNIQUE_CRLF_TEST"
    payload = crlf_input + "\n" + marker_u
    
    full_doc = _read_doc_text(_test_doc)
    new_content = (full_doc + "\n" + payload) if full_doc else payload
    _apply_document_content(_test_doc, _test_ctx, {
        "content": new_content,
        "old_content": full_doc if full_doc else "",
    })

    res_find = _find_text(_test_doc, _test_ctx, {"search": marker_u})
    assert res_find.get("status") == "ok" and res_find.get("ranges"), "Could not find test payload"
    r = res_find["ranges"][0]

    res_find_start = _find_text(_test_doc, _test_ctx, {"search": "Line A"})
    assert res_find_start.get("status") == "ok" and res_find_start.get("ranges"), "Could not find 'Line A' for verification"
    r_start = res_find_start["ranges"][-1]

    total_range_text = _read_doc_text(_test_doc)[r_start["start"]:r["end"]]
    expected_norm = "Line A\nLine B\nUNIQUE_CRLF_TEST"
    assert total_range_text == expected_norm, f"Expected {repr(expected_norm)}, got {repr(total_range_text)}"


@native_test
def test_content_has_markup_auto_detection():
    assert _content_has_markup("**bold**") == True
    assert _content_has_markup("<b>bold</b>") == True
    assert _content_has_markup("# Heading") == True
    assert _content_has_markup("| col1 | col2 |") == True
    assert _content_has_markup("Jane Doe") == False
    assert _content_has_markup("Hello world, this is plain text.") == False
    assert _content_has_markup("") == False
    assert _content_has_markup(None) == False


# Helper: create text with per-character background colors and return the range
COLORS = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF]  # Red Green Blue Yellow Magenta

def _create_colored_text(chars):
    """Insert chars at end of doc, color each char, return an XTextCursor spanning them."""
    text = _test_doc.getText()
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


def _get_char_colors(range_cursor):
    """Read CharBackColor for each character in the range. Returns list of ints."""
    text = _test_doc.getText()
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


@native_test
def test_same_length_replacement_preserves_colors():
    old_chars = "ABCDE"
    rng = _create_colored_text(old_chars)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(old_chars))]
    
    actual_before = _get_char_colors(rng)
    assert actual_before == expected_colors, f"SETUP FAILED: expected {expected_colors} got {actual_before}"

    _replace_text_preserving_format(_test_doc, rng, "PQRST", _test_ctx)
    sd = _test_doc.createSearchDescriptor()
    sd.SearchString = "PQRST"
    found = _test_doc.findFirst(sd)
    assert found, "'PQRST' not found after replace"
    actual_colors = _get_char_colors(found)
    assert actual_colors == expected_colors and found.getString() == "PQRST", f"Expected {expected_colors}, got {actual_colors}, text='{found.getString()}'"


@native_test
def test_longer_replacement_inherits_last_color():
    old_chars = "ABC"
    rng = _create_colored_text(old_chars)
    expected_setup = [COLORS[0], COLORS[1], COLORS[2]]
    actual_before = _get_char_colors(rng)
    assert actual_before == expected_setup, f"SETUP FAILED: expected {expected_setup} got {actual_before}"

    expected_colors = [
        COLORS[0], COLORS[1], COLORS[2],  # overlap: inherit from A, B, C
        COLORS[2], COLORS[2],             # extra chars: inherit from last (C = Blue)
    ]
    _replace_text_preserving_format(_test_doc, rng, "MNOPQ", _test_ctx)
    sd = _test_doc.createSearchDescriptor()
    sd.SearchString = "MNOPQ"
    found = _test_doc.findFirst(sd)
    assert found, "'MNOPQ' not found after replace"
    actual_colors = _get_char_colors(found)
    assert actual_colors == expected_colors and found.getString() == "MNOPQ", f"Expected {expected_colors}, got {actual_colors}, text='{found.getString()}'"


@native_test
def test_shorter_replacement_leftover_deleted():
    old_chars = "ABCDE"
    rng = _create_colored_text(old_chars)
    expected_setup = [COLORS[i % len(COLORS)] for i in range(5)]
    actual_before = _get_char_colors(rng)
    assert actual_before == expected_setup, f"SETUP FAILED: expected {expected_setup} got {actual_before}"

    expected_colors = [COLORS[0], COLORS[1]]  # only first 2 colors survive
    _replace_text_preserving_format(_test_doc, rng, "UV", _test_ctx)
    sd = _test_doc.createSearchDescriptor()
    sd.SearchString = "UV"
    found = _test_doc.findFirst(sd)
    assert found, "'UV' not found after replace"
    actual_colors = _get_char_colors(found)
    result_text = found.getString()
    assert actual_colors == expected_colors and result_text == "UV", f"Expected {expected_colors}, got {actual_colors}, text={repr(result_text)}"


@native_test
def test_long_replacement_process_events():
    long_len = 50
    old_chars = "X" * long_len
    rng = _create_colored_text(old_chars)

    new_chars = "Y" * long_len
    _replace_text_preserving_format(_test_doc, rng, new_chars, _test_ctx)

    sd = _test_doc.createSearchDescriptor()
    sd.SearchString = new_chars
    found = _test_doc.findFirst(sd)
    assert found and found.getString() == new_chars, "Failed to replace %d chars" % long_len


def _insert_colored_word(word):
    """Insert word at end of doc with per-char background colors. Returns (start_offset, end_offset)."""
    text = _test_doc.getText()
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


def _get_colors_at_range(start_off, length):
    """Read CharBackColor for `length` chars starting at absolute offset start_off."""
    text = _test_doc.getText()
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


def _check_colors_at_search(search_str, expected_colors):
    """Find search_str in doc and check its per-char colors."""
    text = _test_doc.getText()
    sd = _test_doc.createSearchDescriptor()
    sd.SearchString = search_str
    found = _test_doc.findFirst(sd)
    if not found:
        return False, "not found in document"
    tmp = text.createTextCursorByRange(found.getStart())
    tmp.gotoStart(True)
    start_off = len(_normalize(tmp.getString()))
    actual = _get_colors_at_range(start_off, len(search_str))
    if actual == expected_colors:
        return True, ""
    return False, "expected %s got %s" % (expected_colors, actual)


@native_test
def test_apply_document_content_target_search_preserves_colors():
    word = "zBertPicklez"   # unique sentinel around the name
    start_off, end_off = _insert_colored_word(word)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": "zBertTicklez",
        "old_content": "zBertPicklez",
    })
    assert result.get("status") == "ok", f"tool target=search failed: {result}"
    ok_flag, detail = _check_colors_at_search("zBertTicklez", expected_colors)
    assert ok_flag, f"colors not preserved: {detail}"


@native_test
def test_apply_document_content_target_range_preserves_colors():
    word = "zNormaFlintez"
    start_off, end_off = _insert_colored_word(word)
    expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

    result = _apply_document_content(_test_doc, _test_ctx, {
        "content": "zNormaGlintez",
        "old_content": "zNormaFlintez",
    })
    assert result.get("status") == "ok", f"tool target=range failed: {result}"
    ok_flag, detail = _check_colors_at_search("zNormaGlintez", expected_colors)
    assert ok_flag, f"colors not preserved: {detail}"


@native_test
def test_apply_document_content_target_full_preserves_colors():
    desktop = get_desktop(_test_ctx)
    small_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
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

        result = _apply_document_content(small_doc, _test_ctx, {
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


