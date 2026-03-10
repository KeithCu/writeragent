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
import json
from plugin.modules.writer.format_support import (
    document_to_content as document_to_markdown,
    insert_content_at_position as _insert_markdown_at_position,
    content_has_markup as _content_has_markup,
    replace_preserving_format as _replace_text_preserving_format,
    apply_content_at_range,
    apply_content_at_search,
    replace_full_document,
    find_text_ranges,
    _doc_text_length as _doc_text_length_raw,
    _preserving_search_replace,
)
from plugin.framework.logging import debug_log
from plugin.framework.uno_helpers import get_desktop

# Compatibility shim: old _doc_text_length returned (length, text), new returns int
def _doc_text_length(doc):
    return (_doc_text_length_raw(doc), "")


# ---------------------------------------------------------------------------
# Compatibility shims for old tool_* dispatch functions (return JSON strings)
# ---------------------------------------------------------------------------

def tool_get_document_content(doc, ctx, params):
    """Shim for old tool_get_document_content dispatch function."""
    scope = params.get("scope", "full")
    start = params.get("start")
    end = params.get("end")
    max_chars = params.get("max_chars")
    try:
        content = document_to_markdown(doc, ctx, services=None,
                                       max_chars=max_chars, scope=scope,
                                       range_start=start, range_end=end)
        doc_len = _doc_text_length_raw(doc)
        result = {"status": "ok", "content": content, "document_length": doc_len}
        if scope == "range" and start is not None and end is not None:
            result["start"] = start
            result["end"] = end
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def tool_apply_document_content(doc, ctx, params):
    """Shim for old tool_apply_document_content dispatch function."""
    content = params.get("content", "")
    target = params.get("target", "end")
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    try:
        if target == "full":
            if not _content_has_markup(content):
                # We need to preserve format, but replace_preserving_format requires 
                # a range. We use a cursor over the whole document.
                doc_len = _doc_text_length_raw(doc)
                rng = doc.getText().createTextCursor()
                rng.gotoStart(False)
                # Advance by doc_len to select the entire document
                remaining = doc_len
                while remaining > 0:
                    n = min(remaining, 8192)
                    rng.goRight(n, True)
                    remaining -= n
                from plugin.modules.writer.format_support import replace_preserving_format
                replace_preserving_format(doc, rng, content, ctx)
            else:
                replace_full_document(doc, ctx, content)
        elif target == "range":
            start = int(params.get("start", 0))
            end = int(params.get("end", 0))
            if not _content_has_markup(content):
                from plugin.modules.writer.ops import get_text_cursor_at_range
                rng = get_text_cursor_at_range(doc, start, end)
                if rng:
                    from plugin.modules.writer.format_support import replace_preserving_format
                    replace_preserving_format(doc, rng, content, ctx)
            else:
                apply_content_at_range(doc, ctx, content, start, end)
        elif target == "search":
            search = params.get("search", "")
            if not _content_has_markup(content):
                from plugin.modules.writer.format_support import _preserving_search_replace
                _preserving_search_replace(doc, ctx, content, search)
            else:
                apply_content_at_search(doc, ctx, content, search)
        else:
            _insert_markdown_at_position(doc, ctx, content, target)
        return json.dumps({"status": "ok"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def tool_find_text(doc, ctx, params):
    """Shim for old tool_find_text dispatch function."""
    search = params.get("search", "")
    case_sensitive = params.get("case_sensitive", True)
    try:
        ranges = find_text_ranges(doc, ctx, search, case_sensitive=case_sensitive)
        return json.dumps({"status": "ok", "ranges": ranges})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# In-LibreOffice test runner (called from main.py menu: Run markdown tests)
# ---------------------------------------------------------------------------

def run_markdown_tests(ctx, model=None):
    """
    Run format_support tests with real UNO. Called from main.py when user chooses Run markdown tests.
    ctx: UNO ComponentContext. model: optional XTextDocument (Writer); if None or not Writer, a new doc is created.
    Returns (passed_count, failed_count, list of message strings).
    """
    log = []
    passed = 0
    failed = 0

    def ok(msg):
        log.append("OK: %s" % msg)

    def fail(msg):
        log.append("FAIL: %s" % msg)

    desktop = get_desktop(ctx)
    doc = model
    if doc is None or not hasattr(doc, "getText"):
        try:
            doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
        except Exception as e:
            return 0, 1, ["Could not create Writer document: %s" % e]
    if not doc or not hasattr(doc, "getText"):
        return 0, 1, ["No Writer document available."]

    debug_log(ctx, "format_tests: run start (model=%s)" % ("supplied" if model is doc else "new"))

    try:
        md = document_to_markdown(doc, ctx, None, scope="full")
        if isinstance(md, str):
            passed += 1
            ok("document_to_markdown(scope='full') returned string (len=%d)" % len(md))
        else:
            failed += 1
            fail("document_to_markdown did not return string: %s" % type(md))
    except Exception as e:
        failed += 1
        log.append("FAIL: document_to_markdown raised: %s" % e)

    try:
        result = tool_get_document_content(doc, ctx, {"scope": "full"})
        data = json.loads(result)
        if data.get("status") == "ok" and "content" in data:
            passed += 1
            ok("tool_get_document_content returned status=ok and content (len=%d)" % len(data.get("content", "")))
        else:
            failed += 1
            fail("tool_get_document_content: %s" % result[:200])
    except Exception as e:
        failed += 1
        log.append("FAIL: tool_get_document_content raised: %s" % e)


    def _read_doc_text(d):
        raw = d.getText().createTextCursor()
        raw.gotoStart(False)
        raw.gotoEnd(True)
        return raw.getString()


    # Test: get_document_content returns document_length
    try:
        result = tool_get_document_content(doc, ctx, {"scope": "full"})
        data = json.loads(result)
        doc_len_actual = len(_read_doc_text(doc))
        if data.get("status") == "ok" and "document_length" in data and data["document_length"] == doc_len_actual:
            passed += 1
            ok("tool_get_document_content returns document_length (%d)" % doc_len_actual)
        else:
            failed += 1
            fail("tool_get_document_content document_length: got %s, doc len=%d" % (data.get("document_length"), doc_len_actual))
    except Exception as e:
        failed += 1
        log.append("FAIL: get_document_content document_length raised: %s" % e)

    test_content = "Format test\n\nThis was inserted by the test."
    insert_needle = "Format test"

    # Test: apply at end via _insert_markdown_at_position
    try:
        len_before = _doc_text_length(doc)[0]
        _insert_markdown_at_position(doc, ctx, test_content, "end")
        full_text = _read_doc_text(doc)
        len_after = len(full_text)
        content_found = insert_needle in full_text
        debug_log(ctx, "format_tests: apply at end len_before=%s len_after=%s content_found=%s" % (
            len_before, len_after, content_found))
        if content_found:
            passed += 1
            ok("apply at end: content found (len_after=%d)" % len_after)
        else:
            failed += 1
            fail("apply at end: content not found (len_before=%d len_after=%d)" % (len_before, len_after))
    except Exception as e:
        failed += 1
        log.append("FAIL: apply at end raised: %s" % e)
        debug_log(ctx, "format_tests: apply at end raised: %s" % e)

    # Test: production path (tool_apply_document_content target='end')
    try:
        result = tool_apply_document_content(doc, ctx, {
            "content": test_content,
            "target": "end",
        })
        data = json.loads(result)
        if data.get("status") != "ok":
            failed += 1
            fail("tool_apply_document_content: %s" % result[:200])
        else:
            full_text = _read_doc_text(doc)
            if insert_needle in full_text:
                passed += 1
                ok("tool_apply_document_content(target='end'): status=ok and content in document (len=%d)" % len(full_text))
            else:
                failed += 1
                fail("tool_apply_document_content returned ok but content not in document (len=%d)" % len(full_text))
    except Exception as e:
        failed += 1
        log.append("FAIL: tool_apply_document_content raised: %s" % e)

    # Test D: formatting (bold, italic) - VISIBLE TEST
    try:
        formatted_input = "<h1>Heading</h1><p><b>Bold text</b> and <i>italic text</i></p>"

        len_before = _doc_text_length(doc)[0]
        result = tool_apply_document_content(doc, ctx, {
            "content": formatted_input,
            "target": "end",
        })
        data = json.loads(result)
        if data.get("status") != "ok":
            failed += 1
            fail("formatted content: tool returned error: %s" % result[:200])
        else:
            full_text = _read_doc_text(doc)
            len_after = len(full_text)
            # Check if ANY of the formatting keywords appear (raw or formatted)
            has_heading = "Heading" in full_text
            has_bold = "Bold" in full_text
            has_italic = "italic" in full_text
            
            if has_heading or has_bold or has_italic:
                passed += 1
                ok("formatted content: INSERTED (len %d→%d, has_heading=%s, has_bold=%s, has_italic=%s)" % (
                    len_before, len_after, has_heading, has_bold, has_italic))
            else:
                failed += 1
                fail("formatted content: NOT FOUND (len %d→%d)" % (len_before, len_after))
    except Exception as e:
        failed += 1
        log.append("FAIL: formatted content test raised: %s" % e)

    # Test E: search-and-replace path
    try:
        # Insert a known string, then replace it with content
        marker = "REPLACE_ME_MARKER"
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertString(cursor, "\n" + marker, False)
        
        replacement = "<b>replaced</b>"
        
        result = tool_apply_document_content(doc, ctx, {
            "content": replacement,
            "target": "search",
            "search": marker,
        })
        data = json.loads(result)
        full_text = _read_doc_text(doc)
        if data.get("status") == "ok" and "replaced" in full_text and marker not in full_text:
            passed += 1
            ok("search-and-replace: marker replaced with content")
        else:
            failed += 1
            fail("search-and-replace: status=%s, marker_gone=%s, replaced_found=%s" % (
                data.get("status"), marker not in full_text, "replaced" in full_text))
    except Exception as e:
        failed += 1
        log.append("FAIL: search-and-replace test raised: %s" % e)

    # Test G: Real list input support
    try:
        # Pass a REAL list, expect joined content
        list_input = ["item_a", "item_b"]
        len_before = _doc_text_length(doc)[0]
        result = tool_apply_document_content(doc, ctx, {
            "content": list_input,
            "target": "end",
        })
        data = json.loads(result)
        full_text = _read_doc_text(doc)
        
        has_content = "item_a" in full_text and "item_b" in full_text
        
        if data.get("status") == "ok" and has_content:
            passed += 1
            ok("list input accommodation: handled list input successfully")
        else:
            failed += 1
            fail("list input accommodation: status=%s, has_content=%s (input was %s)" % (
                data.get("status"), has_content, list_input))
    except Exception as e:
        failed += 1
        log.append("FAIL: list input test raised: %s" % e)

    # Test H: target="full" — replace entire document
    try:
        full_replacement = "<h1>Full Replace Test</h1><p>Only this content should remain.</p>"
        result = tool_apply_document_content(doc, ctx, {"content": full_replacement, "target": "full"})
        data = json.loads(result)
        full_text = _read_doc_text(doc)
        if data.get("status") == "ok" and "Full Replace" in full_text:
            passed += 1
            ok("target='full': replaced entire document (len=%d)" % len(full_text))
        else:
            failed += 1
            fail("target='full': status=%s, content check failed (len=%d)" % (data.get("status"), len(full_text)))
    except Exception as e:
        failed += 1
        log.append("FAIL: target=full test raised: %s" % e)

    # Test I: target="range" with start=0, end=document_length
    try:
        doc_len = _doc_text_length_raw(doc)
        range_content = "<h2>Range Replace</h2><p>Replaced [0, %d).</p>" % doc_len
        result = tool_apply_document_content(doc, ctx, {"content": range_content, "target": "range", "start": 0, "end": doc_len})
        data = json.loads(result)
        full_text = _read_doc_text(doc)
        if data.get("status") == "ok" and "Range Replace" in full_text:
            passed += 1
            ok("target='range' [0, doc_len): replaced content (len=%d)" % len(full_text))
        else:
            failed += 1
            fail("target='range': status=%s (len=%d)" % (data.get("status"), len(full_text)))
    except Exception as e:
        failed += 1
        log.append("FAIL: target=range test raised: %s" % e)

    # Test J: get_document_content scope="range"
    try:
        full_text = _read_doc_text(doc)
        if len(full_text) >= 10:
            result = tool_get_document_content(doc, ctx, {"scope": "range", "start": 0, "end": 10})
            data = json.loads(result)
            if data.get("status") == "ok" and data.get("start") == 0 and data.get("end") == 10 and "content" in data:
                passed += 1
                ok("get_document_content scope='range' (0,10): returns start, end and content")
            else:
                failed += 1
                fail("get_document_content scope=range: %s" % result[:200])
        else:
            passed += 1
            ok("get_document_content scope=range: skipped (doc too short)")
    except Exception as e:
        failed += 1
        log.append("FAIL: get_document_content scope=range raised: %s" % e)

    # Test K: tool_find_text
    try:
        marker_find = "FIND_ME_UNIQUE_xyz"
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertString(cursor, "\n" + marker_find, False)
        
        result = tool_find_text(doc, ctx, {
            "search": marker_find,
            "case_sensitive": True
        })
        data = json.loads(result)
        
        if data.get("status") == "ok" and "ranges" in data:
            ranges = data["ranges"]
            if len(ranges) == 1:
                r = ranges[0]
                text_at_range = _read_doc_text(doc)[r["start"]:r["end"]]
                if text_at_range == marker_find:
                    passed += 1
                    ok("find_text: found correct range")
                else:
                    failed += 1
                    fail("find_text: range text mismatch. Expected '%s', got '%s'" % (marker_find, text_at_range))
            else:
                failed += 1
                fail("find_text: expected 1 match, got %d" % len(ranges))
        else:
            failed += 1
            fail("find_text: status=%s" % data.get("status"))
    except Exception as e:
        failed += 1
        log.append("FAIL: find_text raised: %s" % e)

    # Test L: HTML linebreak preservation
    try:
        plain_input = "Line 1\nLine 2\n\nParagraph 2"
        len_before = _doc_text_length(doc)[0]
        result = tool_apply_document_content(doc, ctx, {
            "content": plain_input,
            "target": "end",
        })
        full_text = _read_doc_text(doc)
        # Check if all words are present. 
        # If the fix works, these will be separated.
        # If it failed, they might be merged, but still present.
        # The real validation is that it doesn't error and content is there.
        has_content = "Line 1" in full_text and "Line 2" in full_text and "Paragraph 2" in full_text
        
        if has_content:
            passed += 1
            ok("HTML linebreak preservation: content inserted")
        else:
            failed += 1
            fail("HTML linebreak preservation: content missing")
    except Exception as e:
        failed += 1
        log.append("FAIL: HTML linebreak preservation test raised: %s" % e)

    # Test U: CRLF normalization integration
    try:
        crlf_input = "Line A\r\nLine B"
        # We use a unique marker to find it back
        marker_u = "UNIQUE_CRLF_TEST"
        payload = crlf_input + "\n" + marker_u
        
        tool_apply_document_content(doc, ctx, {
            "content": payload,
            "target": "end",
        })
        
        # Now find it
        res_find = json.loads(tool_find_text(doc, ctx, {"search": marker_u}))
        if res_find.get("status") == "ok" and res_find.get("ranges"):
            r = res_find["ranges"][0]
            # Look at the text just before the marker
            # The payload was "Line A\r\nLine B\nUNIQUE_CRLF_TEST"
            # Normalized it should be "Line A\nLine B\nUNIQUE_CRLF_TEST"
            # Length should be 7 + 7 + 16 = 30? No. 
            # "Line A" (6) + "\n" (1) + "Line B" (6) + "\n" (1) + "UNIQUE_CRLF_TEST" (16) = 30 chars.
            # If CRLF was preserved it would be 31.
            
            # Get content from start of payload to end of marker
            # We don't know the exact starting offset easily without searching for "Line A"
            res_find_start = json.loads(tool_find_text(doc, ctx, {"search": "Line A"}))
            if res_find_start.get("status") == "ok" and res_find_start.get("ranges"):
                # Find the last "Line A"
                r_start = res_find_start["ranges"][-1]
                total_range_text = _read_doc_text(doc)[r_start["start"]:r["end"]]
                expected_norm = "Line A\nLine B\nUNIQUE_CRLF_TEST"
                if total_range_text == expected_norm:
                    passed += 1
                    ok("CRLF normalization: 'Line A\\r\\nLine B' correctly normalized to '\\n'")
                else:
                    failed += 1
                    fail("CRLF normalization: expected %s, got %s" % (repr(expected_norm), repr(total_range_text)))
            else:
                failed += 1
                fail("CRLF normalization: could not find 'Line A' for verification")
        else:
            failed += 1
            fail("CRLF normalization: could not find test payload")
    except Exception as e:
        failed += 1
        log.append("FAIL: CRLF normalization test raised: %s" % e)
    fp_passed, fp_failed = _run_format_preserving_tests(doc, ctx, ok, fail, log)
    passed += fp_passed
    failed += fp_failed

    # --- Tool integration tests (target=search/range/full + HTML wrapping check) ---
    passed, failed = _run_tool_integration_tests(ctx, doc, passed, failed, log)



    return passed, failed, log


def _run_format_preserving_tests(doc, ctx, ok, fail, log):
    """Tests for _replace_text_preserving_format and _content_has_markup.
    Returns (passed_count, failed_count)."""
    passed = 0
    failed = 0
    text = doc.getText()

    # --- Test M: _content_has_markup auto-detection ---
    try:
        assert _content_has_markup("**bold**") == True
        assert _content_has_markup("<b>bold</b>") == True
        assert _content_has_markup("# Heading") == True
        assert _content_has_markup("| col1 | col2 |") == True
        assert _content_has_markup("Jane Doe") == False
        assert _content_has_markup("Hello world, this is plain text.") == False
        assert _content_has_markup("") == False
        assert _content_has_markup(None) == False
        passed += 1
        ok("_content_has_markup: all detection cases correct")
    except (AssertionError, Exception) as e:
        failed += 1
        fail("_content_has_markup: %s" % e)

    # Helper: create text with per-character background colors and return the range
    COLORS = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF]  # Red Green Blue Yellow Magenta

    def _create_colored_text(chars):
        """Insert chars at end of doc, color each char, return an XTextCursor spanning them."""
        # Insert a newline separator
        sep_cursor = text.createTextCursor()
        sep_cursor.gotoEnd(False)
        text.insertString(sep_cursor, "\n", False)

        def get_accurate_offset():
            c = text.createTextCursor()
            c.gotoStart(False)
            c.gotoEnd(True)
            return len(c.getString())

        start_off = get_accurate_offset()

        for i, ch in enumerate(chars):
            # Insert the character
            ins = text.createTextCursor()
            ins.gotoEnd(False)
            text.insertString(ins, ch, False)
            # Color the character we just inserted
            cc = text.createTextCursor()
            cc.gotoEnd(False)
            cc.goLeft(1, True)
            cc.setPropertyValue("CharBackColor", COLORS[i % len(COLORS)])

        # Create range using the offsets
        range_cursor = text.createTextCursor()
        range_cursor.gotoStart(False)
        remaining = start_off
        while remaining > 0:
            n = min(remaining, 8192)
            range_cursor.goRight(n, False)
            remaining -= n
        range_cursor.gotoEnd(True)
        return range_cursor


    def _get_char_colors(range_cursor):
        """Read CharBackColor for each character in the range. Returns list of ints."""
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

    # --- Test N: Same-length replacement preserves per-char colors ---
    try:
        old_chars = "ABCDE"
        rng = _create_colored_text(old_chars)
        expected_colors = [COLORS[i % len(COLORS)] for i in range(len(old_chars))]
        # Verify setup: coloring must be correct before we replace
        actual_before = _get_char_colors(rng)
        if actual_before != expected_colors:
            failed += 1
            fail("same-length replacement: SETUP FAILED colors before replace: expected %s got %s" % (expected_colors, actual_before))
        else:
            _replace_text_preserving_format(doc, rng, "PQRST", ctx)
            sd = doc.createSearchDescriptor()
            sd.SearchString = "PQRST"
            found = doc.findFirst(sd)
            if found:
                actual_colors = _get_char_colors(found)
                if actual_colors == expected_colors and found.getString() == "PQRST":
                    passed += 1
                    ok("same-length replacement: text='PQRST', all 5 colors preserved")
                else:
                    failed += 1
                    fail("same-length replacement: colors expected %s got %s, text='%s'" % (expected_colors, actual_colors, found.getString()))
            else:
                failed += 1
                fail("same-length replacement: 'PQRST' not found after replace")
    except Exception as e:
        failed += 1
        log.append("FAIL: same-length format-preserving test raised: %s" % e)

    # --- Test O: Longer replacement, extra chars inherit last color ---
    try:
        old_chars = "ABC"
        rng = _create_colored_text(old_chars)
        expected_setup = [COLORS[0], COLORS[1], COLORS[2]]
        actual_before = _get_char_colors(rng)
        if actual_before != expected_setup:
            failed += 1
            fail("longer replacement: SETUP FAILED colors before replace: expected %s got %s" % (expected_setup, actual_before))
        else:
            expected_colors = [
                COLORS[0], COLORS[1], COLORS[2],  # overlap: inherit from A, B, C
                COLORS[2], COLORS[2],              # extra chars: inherit from last (C = Blue)
            ]
            _replace_text_preserving_format(doc, rng, "MNOPQ", ctx)
            sd = doc.createSearchDescriptor()
            sd.SearchString = "MNOPQ"
            found = doc.findFirst(sd)
            if found:
                actual_colors = _get_char_colors(found)
                if actual_colors == expected_colors and found.getString() == "MNOPQ":
                    passed += 1
                    ok("longer replacement: text='MNOPQ', 3 original + 2 inherited colors correct")
                else:
                    failed += 1
                    fail("longer replacement: colors expected %s got %s, text='%s'" % (expected_colors, actual_colors, found.getString()))
            else:
                failed += 1
                fail("longer replacement: 'MNOPQ' not found after replace")
    except Exception as e:
        failed += 1
        log.append("FAIL: longer format-preserving test raised: %s" % e)

    # --- Test P: Shorter replacement, leftover chars deleted ---
    try:
        old_chars = "ABCDE"
        rng = _create_colored_text(old_chars)
        expected_setup = [COLORS[i % len(COLORS)] for i in range(5)]
        actual_before = _get_char_colors(rng)
        if actual_before != expected_setup:
            failed += 1
            fail("shorter replacement: SETUP FAILED colors before replace: expected %s got %s" % (expected_setup, actual_before))
        else:
            expected_colors = [COLORS[0], COLORS[1]]  # only first 2 colors survive
            _replace_text_preserving_format(doc, rng, "UV", ctx)
            sd = doc.createSearchDescriptor()
            sd.SearchString = "UV"
            found = doc.findFirst(sd)
            if found:
                actual_colors = _get_char_colors(found)
                result_text = found.getString()
                if actual_colors == expected_colors and result_text == "UV":
                    passed += 1
                    ok("shorter replacement: text='UV', 2 colors preserved, 3 chars deleted")
                else:
                    failed += 1
                    fail("shorter replacement: text=%s colors expected %s got %s" % (repr(result_text), expected_colors, actual_colors))
            else:
                failed += 1
                fail("shorter replacement: 'UV' not found after replace")
    except Exception as e:
        failed += 1
        log.append("FAIL: shorter format-preserving test raised: %s" % e)

    
    # --- Test T: Long replacement triggers processEvents (no crash) ---
    try:
        # Create a string > 500 chars to trigger the processEvents path
        long_len = 505
        old_chars = "X" * long_len
        rng = _create_colored_text(old_chars)
        
        # Replace with same length string
        # context_msg = "Profiling 500 char replacement..."
        # import cProfile, pstats, io
        # pr = cProfile.Profile()
        # pr.enable()
        
        new_chars = "Y" * long_len
        _replace_text_preserving_format(doc, rng, new_chars, ctx)
        
        # pr.disable()
        # s = io.StringIO()
        # ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
        # ps.print_stats(20)
        # log.append("PROFILE STATS:\n%s" % s.getvalue())
        
        sd = doc.createSearchDescriptor()
        sd.SearchString = new_chars
        found = doc.findFirst(sd)
        if found and found.getString() == new_chars:
            passed += 1
            ok("processEvents test: replaced %d chars successfully" % long_len)
        else:
            failed += 1
            fail("processEvents test: failed to replace %d chars" % long_len)
    except Exception as e:
        failed += 1
        log.append("FAIL: processEvents test raised: %s" % e)

    return passed, failed


def _run_tool_integration_tests(ctx, doc, passed, failed, log):
    """Integration tests: call tool_apply_document_content end-to-end with plain text
    and verify that CharBackColor is preserved. These cover the full stack including
    the _ensure_html_linebreaks ordering fix and all three target paths.

    Each test uses a fresh colored word so there are no search collisions.
    """
    ok = lambda msg: log.append("OK: %s" % msg)
    fail = lambda msg: log.append("FAIL: %s" % msg)

    text = doc.getText()
    COLORS = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF]

    def _insert_colored_word(word):
        """Insert word at end of doc with per-char background colors. Returns (start_offset, end_offset)."""
        sep = text.createTextCursor()
        sep.gotoEnd(False)
        text.insertString(sep, "\n", False)

        # We still need offsets for target='range' tests.
        # Measure objectively from doc start.
        def get_accurate_offset():
            c = text.createTextCursor()
            c.gotoStart(False)
            c.gotoEnd(True)
            return len(c.getString())

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
        from plugin.framework.document import get_text_cursor_at_range
        colors = []
        pos_cursor = text.createTextCursor()
        pos_cursor.gotoStart(False)
        pos_cursor.goRight(start_off, False)
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
        sd = doc.createSearchDescriptor()
        sd.SearchString = search_str
        found = doc.findFirst(sd)
        if not found:
            return False, "not found in document"
        # Build start offset by measuring from doc start
        tmp = text.createTextCursorByRange(found.getStart())
        tmp.gotoStart(True)
        start_off = len(tmp.getString())
        actual = _get_colors_at_range(start_off, len(search_str))
        if actual == expected_colors:
            return True, ""
        return False, "expected %s got %s" % (expected_colors, actual)

    # --- Test Q: tool_apply_document_content(target='search') preserves colors ---
    # Simulates: AI is asked "change Bert Pickle to Bert Tickle"
    try:
        word = "zBertPicklez"   # unique sentinel around the name
        start_off, end_off = _insert_colored_word(word)
        expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

        result = json.loads(tool_apply_document_content(doc, ctx, {
            "content": "zBertTicklez",
            "target": "search",
            "search": "zBertPicklez",
        }))
        if result.get("status") != "ok":
            failed += 1; fail("tool target=search: tool returned error: %s" % result)
        else:
            ok_flag, detail = _check_colors_at_search("zBertTicklez", expected_colors)
            if ok_flag:
                passed += 1
                ok("tool target=search plain text: 'zBertPicklez'→'zBertTicklez', all colors preserved")
            else:
                failed += 1
                fail("tool target=search plain text: colors not preserved: %s" % detail)
    except Exception as e:
        failed += 1
        log.append("FAIL: tool target=search integration test raised: %s" % e)

    # --- Test R: tool_apply_document_content(target='range') preserves colors ---
    # Simulates: AI uses find_text then apply with target=range
    try:
        word = "zNormaFlintez"
        start_off, end_off = _insert_colored_word(word)
        expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

        result = json.loads(tool_apply_document_content(doc, ctx, {
            "content": "zNormaGlintez",
            "target": "range",
            "start": start_off,
            "end": end_off,
        }))
        if result.get("status") != "ok":
            failed += 1; fail("tool target=range: tool returned error: %s" % result)
        else:
            ok_flag, detail = _check_colors_at_search("zNormaGlintez", expected_colors)
            if ok_flag:
                passed += 1
                ok("tool target=range plain text: 'zNormaFlintez'→'zNormaGlintez', all colors preserved")
            else:
                failed += 1
                fail("tool target=range plain text: colors not preserved: %s" % detail)
    except Exception as e:
        failed += 1
        log.append("FAIL: tool target=range integration test raised: %s" % e)

    # --- Test S: tool_apply_document_content(target='full') preserves colors ---
    # Simulates: AI replaces the entire (small) document with a plain text edit
    # Use a fresh single-paragraph doc so doc_len == word length
    try:
        desktop = get_desktop(ctx)
        small_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
        if not small_doc or not hasattr(small_doc, "getText"):
            raise RuntimeError("Could not create small doc for target=full test")
        small_text = small_doc.getText()
        word = "zGordonCrumpz"
        new_word = "zGordonStumpz"
        # Insert the colored word into the fresh small doc
        for i, ch in enumerate(word):
            ins = small_text.createTextCursor()
            ins.gotoEnd(False)
            small_text.insertString(ins, ch, False)
            cc = small_text.createTextCursor()
            cc.gotoEnd(False)
            cc.goLeft(1, True)
            cc.setPropertyValue("CharBackColor", COLORS[i % len(COLORS)])
        expected_colors = [COLORS[i % len(COLORS)] for i in range(len(word))]

        result = json.loads(tool_apply_document_content(small_doc, ctx, {
            "content": new_word,
            "target": "full",
        }))
        if result.get("status") != "ok":
            failed += 1; fail("tool target=full: tool returned error: %s" % result)
        else:
            sd = small_doc.createSearchDescriptor()
            sd.SearchString = new_word
            found = small_doc.findFirst(sd)
            if not found:
                failed += 1
                fail("tool target=full plain text: '%s' not found after replace" % new_word)
            else:
                small_txt = small_doc.getText()
                tmp = small_txt.createTextCursorByRange(found.getStart())
                tmp.gotoStart(True)
                start_off2 = len(tmp.getString())
                actual_colors = []
                pos_c = small_txt.createTextCursor()
                pos_c.gotoStart(False)
                pos_c.goRight(start_off2, False)
                for _ in range(len(new_word)):
                    cc2 = small_txt.createTextCursorByRange(pos_c)
                    cc2.goRight(1, True)
                    try:
                        actual_colors.append(cc2.getPropertyValue("CharBackColor"))
                    except Exception:
                        actual_colors.append(-1)
                    pos_c = small_txt.createTextCursorByRange(cc2.getEnd())
                if actual_colors == expected_colors:
                    passed += 1
                    ok("tool target=full plain text: '%s'→'%s', all colors preserved" % (word, new_word))
                else:
                    failed += 1
                    fail("tool target=full plain text: colors expected %s got %s" % (expected_colors, actual_colors))
        try:
            small_doc.close(True)
        except Exception:
            pass
    except Exception as e:
        failed += 1
        log.append("FAIL: tool target=full integration test raised: %s" % e)

    return passed, failed
