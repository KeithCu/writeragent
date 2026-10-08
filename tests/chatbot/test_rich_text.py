# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for plugin.chatbot.rich_text (append_rich_text, theme colors, HTML detection)."""

from unittest.mock import MagicMock, patch
import pytest


class MockTextCursor:
    """Minimal mock for XTextCursor used by append_rich_text."""

    def __init__(self):
        self._pos = 0
        self.CharHeight = None
        self.CharWeight = None
        self.CharColor = None
        self.CharFontName = None
        self.CharBackColor = None

    def gotoEnd(self, select):
        pass

    def gotoStart(self, select):
        pass

    def goRight(self, count, select):
        if not hasattr(self, "go_right_calls"):
            self.go_right_calls = []
        self.go_right_calls.append((count, select))
        return True

    def getStart(self):
        return self

    def gotoRange(self, target, select):
        pass

    def insertDocumentFromURL(self, url, props):
        pass

    def goLeft(self, count, select):
        pass


class MockText:
    """Minimal mock for XText."""

    def __init__(self):
        self._content = ""
        self._cursor = MockTextCursor()

    def createTextCursor(self):
        return self._cursor

    def createTextCursorByRange(self, rng):
        return MockTextCursor()

    def getString(self):
        return self._content

    def setString(self, s):
        self._content = s

    def insertString(self, cursor, text, absorb):
        self._content += text


class MockDoc:
    """Minimal mock for a Writer document used by append_rich_text."""

    def __init__(self):
        self._text = MockText()
        self._controller = MagicMock()

    @property
    def CharacterCount(self):
        return len(self._text._content)

    def getText(self):
        return self._text

    def getCurrentController(self):
        return self._controller


class TestAppendRichText:
    """Tests for append_rich_text formatting logic."""

    def _call(self, text, role="assistant"):
        from plugin.chatbot.rich_text import append_rich_text

        doc = MockDoc()
        append_rich_text(doc, text, role=role)
        return doc

    @pytest.mark.parametrize(
        "value, role, value_2, value_3",
        [
            pytest.param("Hello", "user", "You: ", "Hello", id="test_user_role_prefix"),
            pytest.param("World", "assistant", "Assistant: ", "World", id="test_assistant_role_prefix"),
        ],
    )
    def test_user_role_prefix(self, value, role, value_2, value_3):
        doc = self._call(value, role=role)
        content = doc.getText().getString()
        assert (value_2) in (content)
        assert (value_3) in (content)


    def test_assistant_role_prefix_uses_gettext(self):
        with patch("plugin.chatbot.rich_text._", side_effect=lambda message: "アシスタント:" if message == "Assistant:" else message):
            doc = self._call("World", role="assistant")
        content = doc.getText().getString()
        assert "アシスタント: World" in content
        assert "Assistant:" not in content

    @pytest.mark.parametrize(
        "value, value_2",
        [
            # Non-HTML text is inserted via insertString (no HTML import).
            pytest.param("Just some text", "Just some text", id="test_plain_text_inserted_for_non_html"),
            pytest.param("", "Assistant: ", id="test_empty_text"),
        ],
    )
    def test_plain_text_inserted_for_non_html(self, value, value_2):
        doc = self._call(value, role="assistant")
        content = doc.getText().getString()
        assert (value_2) in (content)


    def test_go_right_chunks_large_moves(self):
        from plugin.chatbot.rich_text import _go_right

        cursor = MockTextCursor()
        assert _go_right(cursor, 40000, False) is True
        assert hasattr(cursor, "go_right_calls")

        calls = cursor.go_right_calls
        pre_len = sum(count for count, expand in calls)
        assert pre_len > 32767, f"pre_len {pre_len} was not large enough"
        assert all(count <= 8192 for count, expand in calls)

    def test_append_rich_text_anchors_body_start_without_character_count_offset(self):
        """The body range starts one past the prefix's last character, not at a document-start count."""
        from plugin.chatbot.rich_text import append_rich_text, USER_COLOR

        doc = MockDoc()
        append_rich_text(doc, "done", role="assistant")

        by_range = []
        original_by_range = doc.getText().createTextCursorByRange

        def patched_by_range(rng):
            c = original_by_range(rng)
            by_range.append(c)
            return c

        doc.getText().createTextCursorByRange = patched_by_range

        append_rich_text(doc, "how are you?", role="user")

        body_range = by_range[-1]
        assert body_range.go_right_calls == [(1, False)]
        assert body_range.CharColor == USER_COLOR

    def test_user_color(self):
        """Verify the prefix cursor gets USER_COLOR via createTextCursorByRange."""
        from plugin.chatbot.rich_text import USER_COLOR

        doc = MockDoc()
        created_cursors = []
        doc.getText().createTextCursorByRange

        def track_cursor(rng):
            c = MockTextCursor()
            created_cursors.append(c)
            return c

        doc.getText().createTextCursorByRange = track_cursor
        from plugin.chatbot.rich_text import append_rich_text
        append_rich_text(doc, "hi", role="user")
        prefix_cursor = created_cursors[0]
        assert (prefix_cursor.CharColor) == (USER_COLOR)

    def test_assistant_color_is_deep_slate_gray(self):
        from plugin.chatbot.rich_text import ASSISTANT_COLOR

        assert (ASSISTANT_COLOR) == (0x1E293B)

    def test_user_color_is_indigo_blue(self):
        from plugin.chatbot.rich_text import USER_COLOR

        assert (USER_COLOR) == (0x2A6099)

    def test_get_theme_colors_light_mode(self):
        """get_theme_colors returns light palette for high luminance background."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        style_settings = MagicMock()
        style_settings.FieldColor = 0xFFFFFF  # White background
        style_settings.DialogColor = 0xEFF0F1 # Light gray dialog
        doc.getCurrentController().getFrame().getContainerWindow().StyleSettings = style_settings

        bg_color, user_color, assistant_color = get_theme_colors(doc)
        assert (bg_color) == (0xE0E1E2)
        assert (user_color) == (0x2A6099)
        assert (assistant_color) == (0x1E293B)

    def test_get_theme_colors_dark_mode(self):
        """get_theme_colors returns dark palette for low luminance background."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        style_settings = MagicMock()
        style_settings.FieldColor = 0x1E1E1E  # Dark background
        doc.getCurrentController().getFrame().getContainerWindow().StyleSettings = style_settings

        bg_color, user_color, assistant_color = get_theme_colors(doc)
        assert (bg_color) == (0x1E1E1E)
        assert (user_color) == (0x60A5FA)
        assert (assistant_color) == (0xE2E8F0)

    def test_get_theme_colors_from_style_window(self):
        """get_theme_colors can read StyleSettings directly from the sidebar window."""
        from plugin.chatbot.rich_text import get_theme_colors

        style_window = MagicMock()
        style_settings = MagicMock()
        style_settings.FieldColor = 0x1E1E1E
        style_window.StyleSettings = style_settings

        bg_color, user_color, assistant_color = get_theme_colors(style_window=style_window)
        assert (bg_color) == (0x1E1E1E)
        assert (user_color) == (0x60A5FA)
        assert (assistant_color) == (0xE2E8F0)

    def test_get_theme_colors_graceful_fallback(self):
        """get_theme_colors returns standard light palette when window or StyleSettings are missing/mocked."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        # Missing Frame / Container Window (getCurrentController returns MagicMock, which returns MagicMock)
        bg_color, user_color, assistant_color = get_theme_colors(doc)
        assert (bg_color) == (0xE0E1E2)
        assert (user_color) == (0x2A6099)
        assert (assistant_color) == (0x1E293B)

    def test_append_rich_text_uses_dynamic_dark_colors(self):
        """append_rich_text formats role prefix using dynamic dark mode colors."""
        from plugin.chatbot.rich_text import append_rich_text
        doc = MockDoc()
        style_settings = MagicMock()
        style_settings.FieldColor = 0x1E1E1E  # Dark mode
        doc.getCurrentController().getFrame().getContainerWindow().StyleSettings = style_settings

        created_cursors = []
        def track_cursor(rng):
            c = MockTextCursor()
            created_cursors.append(c)
            return c
        doc.getText().createTextCursorByRange = track_cursor

        append_rich_text(doc, "hi", role="user")
        prefix_cursor = created_cursors[0]
        assert (prefix_cursor.CharColor) == (0x60A5FA)  # Dark-mode-optimized user blue

    def test_html_body_preserves_span_colors(self):
        """Successful HTML import must not blanket-overwrite body CharColor."""
        from plugin.chatbot.rich_text import append_rich_text

        doc = MockDoc()
        body_cursors = []

        def track_body_cursor(rng):
            c = MockTextCursor()
            body_cursors.append(c)
            return c

        # The body range is created from the prefix anchor, by range.
        doc.getText().createTextCursorByRange = track_body_cursor

        with patch("plugin.chatbot.rich_text._insert_html_at_cursor"):
            append_rich_text(doc, '<p><span style="color:#ff0000">red</span></p>', role="assistant")

        assert (len(body_cursors)) >= (2)
        assert (body_cursors[-1].CharColor) is None

    def test_plain_body_gets_role_color(self):
        """Non-HTML body still receives the role tint."""
        from plugin.chatbot.rich_text import append_rich_text, ASSISTANT_COLOR

        doc = MockDoc()
        body_cursors = []

        def track_body_cursor(rng):
            c = MockTextCursor()
            body_cursors.append(c)
            return c

        # The body range is created from the prefix anchor, by range.
        doc.getText().createTextCursorByRange = track_body_cursor
        append_rich_text(doc, "plain answer", role="assistant")

        assert (len(body_cursors)) >= (2)
        assert (body_cursors[-1].CharColor) == (ASSISTANT_COLOR)

    def test_html_import_failure_does_not_insert_raw_tags(self):
        """A filter exception must not leave the tags in the hidden doc."""
        from plugin.chatbot.rich_text import append_rich_text

        doc = MockDoc()
        with patch(
            "plugin.chatbot.rich_text._insert_html_at_cursor",
            side_effect=RuntimeError("filter"),
        ):
            ok = append_rich_text(doc, "<p>Hi</p>", role="assistant")

        assert ok is False
        content = doc.getText().getString()
        # The prefix was inserted before the filter raised. Restoring the body
        # drops that partial row; the caller writes the stripped message.
        assert content == ""
        assert "<p>" not in content
        assert "Hi" not in content

    def test_full_document_imports_body_only(self):
        """A full HTML document is reduced to its body before the filter."""
        from plugin.chatbot.rich_text import append_rich_text

        doc = MockDoc()
        seen: list[str] = []

        def _capture(_doc, _cursor, fragment):
            seen.append(fragment)

        full = "<html><head><script>alert(1)</script></head><body><p>Hi</p></body></html>"
        with patch("plugin.chatbot.rich_text._insert_html_at_cursor", side_effect=_capture):
            ok = append_rich_text(doc, full, role="assistant")

        assert ok is True
        assert seen == ["<p>Hi</p>"]

    def test_bad_element_does_not_leave_tags_or_a_partial_row(self):
        """A filter that writes tags and then raises must not leave them."""
        from plugin.chatbot.rich_text import append_rich_text, render_messages_to_hidden_doc

        doc = MockDoc()

        def _writes_tags_then_raises(_doc, _cursor, fragment):
            doc.getText().insertString(None, fragment, False)
            raise RuntimeError("bad element")

        with patch("plugin.chatbot.rich_text._insert_html_at_cursor", side_effect=_writes_tags_then_raises):
            ok = append_rich_text(doc, "<p>Hi</p><script>alert(1)</script>", role="assistant")

        assert ok is False
        assert "<" not in doc.getText().getString()

        doc = MockDoc()
        calls = {"n": 0}

        def _later_edit_inserts_foreign_text(_doc, _cursor, fragment):
            del fragment
            calls["n"] += 1
            doc.getText().insertString(None, "NOT_IN_MESSAGES", False)
            raise RuntimeError("later edit failed")

        with patch("plugin.chatbot.rich_text._insert_html_at_cursor", side_effect=_later_edit_inserts_foreign_text):
            render_messages_to_hidden_doc(
                doc,
                [("user", "keep me"), ("assistant", "<p>second</p>")],
            )

        content = doc.getText().getString()
        assert "NOT_IN_MESSAGES" not in content
        assert "keep me" in content
        assert "second" in content
        assert "<p>" not in content
        assert calls["n"] == 1

    def test_render_rejects_an_edit_that_is_not_the_message(self):
        """A later edit that returns success but wrote other text must not stay."""
        from plugin.chatbot.rich_text import render_messages_to_hidden_doc

        doc = MockDoc()

        def _lie(target, text, role="assistant", style_window=None):
            del text, role, style_window
            target.getText().insertString(None, "NOT_IN_MESSAGES", False)
            return True

        with patch("plugin.chatbot.rich_text.append_rich_text", side_effect=_lie):
            render_messages_to_hidden_doc(doc, [("assistant", "hello")])

        content = doc.getText().getString()
        assert "NOT_IN_MESSAGES" not in content
        assert "hello" in content


class TestLeadingListImport:
    """A reply that opens with <ol>/<ul> lost its first item's number (release QA area 5).

    Writer pastes the first imported paragraph into the "Assistant: "
    paragraph, so the first <li> became plain text and the rest started at 1.
    """

    def _imported(self, html):
        from plugin.chatbot.rich_text import append_rich_text

        seen: list[str] = []

        def _capture(_doc, _cursor, fragment):
            seen.append(fragment)

        with patch("plugin.chatbot.rich_text._insert_html_at_cursor", side_effect=_capture):
            assert append_rich_text(MockDoc(), html, role="assistant")
        return seen

    @pytest.mark.parametrize(
        "value, value_2",
        [
            pytest.param("<ol><li>a</li><li>b</li></ol>", "<p>\u200b</p><ol><li>a</li><li>b</li></ol>", id="test_leading_ordered_list_gets_sentinel_paragraph"),
            pytest.param("<p>Here:</p><ol><li>a</li></ol>", "<p>Here:</p><ol><li>a</li></ol>", id="test_intro_paragraph_keeps_sharing_the_prefix_line"),
        ],
    )
    def test_leading_ordered_list_gets_sentinel_paragraph(self, value, value_2):
        assert self._imported(value) == [value_2]

    def test_leading_list_with_whitespace_and_attributes(self):
        seen = self._imported('\n  <OL start="11">\n<li>a</li></OL>')
        assert seen[0].startswith("<p>\u200b</p>")
        seen = self._imported("<ul><li>a</li></ul>")
        assert seen[0].startswith("<p>\u200b</p><ul>")

    def test_numbered_paragraphs_start_on_their_own_line(self):
        """gpt-oss-20b sent <p>1. ...</p><p>2. ...</p>; item 1 sat on the label line."""
        html = "<p>1. Lighthouses use a lens.</p>\n<p>2. Colors matter.</p>"
        assert self._imported(html) == ["<p>\u200b</p>" + html]
        for lead in ("\n<p>6. Six</p>", '<p class="x"> 2) Two</p>', "<p>\u2022 dot</p>", "<p>- dash</p>"):
            assert self._imported(lead)[0].startswith("<p>\u200b</p>"), lead

    def test_prose_paragraphs_keep_sharing_the_prefix_line(self):
        for html in ("<p>2024 was a good year.</p>", "<p>**Bold** text</p>", "<p>1.5 million keepers</p>"):
            assert self._imported(html) == [html], html


    def test_sentinel_is_deleted_after_import(self):
        from plugin.chatbot.rich_text import _drop_list_sentinel

        class Probe:
            def __init__(self, char):
                self.char = char

            def goRight(self, count, select):
                return True

            def getString(self):
                return self.char

            def setString(self, value):
                self.char = value

        anchor = MagicMock()
        for char, expected in (("\u200b", ""), ("x", "x")):
            probe = Probe(char)
            text_obj = MagicMock()
            text_obj.createTextCursorByRange.return_value = probe
            _drop_list_sentinel(text_obj, anchor)
            assert probe.char == expected

    def test_compact_list_matches_its_message(self):
        """A full repaint checked the span against "abc" and wrote plain "abc"."""
        from plugin.chatbot.rich_text import _span_matches_message

        html = "<ol><li>alpha</li><li>beta</li><li>gamma</li></ol>"
        assert _span_matches_message("Assistant: \nalpha\nbeta\ngamma\n", "assistant", html)
        assert _span_matches_message("Assistant: \na\nb\nc\n", "assistant", "<ul><li>a</li><li>b</li><li>c</li></ul>")
        assert not _span_matches_message("Assistant: alpha\nINTRUDER", "assistant", html)


class TestTightenListIndent:
    """Tests for _tighten_list_indent post-processing helper."""

    def _make_list_para(self, text="• item", level=0, list_id="list1", is_number=True):
        """Create a mock paragraph that uses NumberingRules."""
        import sys
        sys.modules["uno"]

        para = MagicMock()
        props = {
            "NumberingIsNumber": is_number,
            "NumberingLevel": level,
            "ListId": list_id,
        }
        para.getPropertyValue.side_effect = lambda name: props[name]
        para.getString.return_value = text

        rule_prop_left = MagicMock()
        rule_prop_left.Name = "LeftMargin"
        rule_prop_left.Value = 635

        rule_prop_flo = MagicMock()
        rule_prop_flo.Name = "FirstLineOffset"
        rule_prop_flo.Value = -635

        rule_prop_other = MagicMock()
        rule_prop_other.Name = "BulletChar"
        rule_prop_other.Value = "\u2022"

        rules = MagicMock()
        rules.getByIndex.return_value = [rule_prop_left, rule_prop_flo, rule_prop_other]
        props["NumberingRules"] = rules

        return para, rules

    def _make_body_range(self, paragraphs):
        """Create a mock body_range whose createEnumeration yields paragraphs."""
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True] * len(paragraphs) + [False]
        enum.nextElement.side_effect = paragraphs
        body_range = MagicMock()
        body_range.createEnumeration.return_value = enum
        return body_range

    def test_tightens_list_paragraph(self):
        import sys
        mock_uno = sys.modules["uno"]
        mock_uno.Any.side_effect = lambda type_str, val: val
        mock_uno.invoke.side_effect = lambda obj, method, args: None
        mock_uno.invoke.reset_mock()

        from plugin.chatbot.rich_text import _tighten_list_indent

        para, rules = self._make_list_para(level=0)
        body_range = self._make_body_range([para])

        _tighten_list_indent(body_range)

        mock_uno.invoke.assert_called_once()

    def test_skips_non_list_paragraph(self):
        import sys
        mock_uno = sys.modules["uno"]
        mock_uno.invoke.reset_mock()

        from plugin.chatbot.rich_text import _tighten_list_indent

        para, _ = self._make_list_para(is_number=False)
        body_range = self._make_body_range([para])

        _tighten_list_indent(body_range)

        mock_uno.invoke.assert_not_called()

    def test_deduplicates_by_list_id_and_level(self):
        import sys
        mock_uno = sys.modules["uno"]
        mock_uno.Any.side_effect = lambda type_str, val: val
        mock_uno.invoke.side_effect = lambda obj, method, args: None
        mock_uno.invoke.reset_mock()

        from plugin.chatbot.rich_text import _tighten_list_indent

        para1, _ = self._make_list_para(text="item 1", level=0, list_id="same")
        para2, _ = self._make_list_para(text="item 2", level=0, list_id="same")
        body_range = self._make_body_range([para1, para2])

        _tighten_list_indent(body_range)

        assert (mock_uno.invoke.call_count) == (1)

    def test_processes_different_levels(self):
        import sys
        mock_uno = sys.modules["uno"]
        mock_uno.Any.side_effect = lambda type_str, val: val
        mock_uno.invoke.side_effect = lambda obj, method, args: None
        mock_uno.invoke.reset_mock()

        from plugin.chatbot.rich_text import _tighten_list_indent

        para1, _ = self._make_list_para(level=0, list_id="L1")
        para2, _ = self._make_list_para(level=1, list_id="L1")
        body_range = self._make_body_range([para1, para2])

        _tighten_list_indent(body_range)

        assert (mock_uno.invoke.call_count) == (2)


class TestHtmlDetectionRegex:
    """Tests for _HTML_TAG_RE used in append_rich_text HTML detection."""

    def _matches(self, text):
        from plugin.chatbot.rich_text import _HTML_TAG_RE
        return bool(_HTML_TAG_RE.search(text))

    # --- True positives ---

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("<p>hello</p>", id="test_p_tag"),
            pytest.param('<p class="intro">text</p>', id="test_p_with_attrs"),
            pytest.param("<br/>", id="test_br_self_closing"),
            pytest.param("<br />", id="test_br_space_closing"),
            pytest.param("<BR>", id="test_br_uppercase"),
            pytest.param("</h1>", id="test_closing_h1"),
            pytest.param("</h2>", id="test_closing_h2"),
            pytest.param("</h6>", id="test_closing_h6"),
            pytest.param("<ul>", id="test_ul"),
            pytest.param("<OL>", id="test_ol_uppercase"),
            pytest.param("<li>", id="test_li"),
            pytest.param("<strong>bold</strong>", id="test_strong"),
            pytest.param("<Strong>text</Strong>", id="test_strong_mixed_case"),
            pytest.param("<em>italic</em>", id="test_em"),
            pytest.param("<code>x</code>", id="test_code"),
            pytest.param("<pre>block</pre>", id="test_pre"),
            pytest.param("<div>content</div>", id="test_div"),
            pytest.param("<table>", id="test_table"),
            pytest.param("some text\n<ul>\n<li>item</li>\n</ul>", id="test_html_embedded_in_prose"),
            pytest.param("<P>", id="test_p_all_uppercase"),
            pytest.param("<div>first thing", id="test_tag_at_start"),
            pytest.param("last thing<br/>", id="test_tag_at_end"),
        ],
    )
    def test_p_tag(self, value):
        assert (self._matches(value))

    # --- True negatives ---

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("Hello world", id="test_plain_text"),
            pytest.param("a < b and c > d", id="test_math_comparisons"),
            pytest.param("3 < 5 and 10 > 7", id="test_numeric_comparisons"),
            pytest.param("<prevent>", id="test_prevent_not_p"),
            pytest.param("<tablet>", id="test_tablet_not_table"),
            pytest.param("Use <preview> mode", id="test_preview_not_pre"),
            pytest.param("<coding>", id="test_coding_not_code"),
            pytest.param("the <olive> tree", id="test_olive_not_ol"),
            pytest.param("", id="test_empty_string"),
            pytest.param("email@<domain>", id="test_email_angle_brackets"),
            pytest.param("a < b", id="test_lt_without_gt"),
            pytest.param("<emphasis>", id="test_emphasis_not_em"),
            pytest.param("<listing>", id="test_listing_not_li"),
            pytest.param("<division>", id="test_division_not_div"),
        ],
    )
    def test_plain_text(self, value):
        assert not (self._matches(value))

    # --- Edge cases ---

    def test_large_plain_text(self):
        assert not (self._matches("x" * 1_000_000))

    def test_large_text_with_tag_at_end(self):
        assert (self._matches("x" * 1_000_000 + "<p>"))


class TestContainsHtmlTag:
    """Real tags, not every ``<Letter…>`` token."""

    def test_generic_url_and_email_are_not_tags(self):
        from plugin.chatbot.rich_text import contains_html_tag

        assert contains_html_tag("Use List<String> here") is False
        assert contains_html_tag("Write <user@example.com> today") is False
        assert contains_html_tag("See <https://example.com/a>") is False
        assert contains_html_tag("3 < 5") is False
        assert contains_html_tag("<prevent>") is False

    def test_formatting_script_and_declarations_are_tags(self):
        from plugin.chatbot.rich_text import contains_html_tag

        assert contains_html_tag("<b>bold</b>") is True
        assert contains_html_tag("<i>italic</i>") is True
        assert contains_html_tag("<script>alert(1)</script>") is True
        assert contains_html_tag("<p>Hi</p>") is True
        assert contains_html_tag("<!-- note -->") is True
        assert contains_html_tag("<br/>") is True
        assert contains_html_tag("<widget/>") is True


class TestGenericTokensStayInTheTranscript:
    def test_append_and_render_keep_tokens_and_still_import_formatting(self):
        from plugin.chatbot.rich_text import append_rich_text, render_messages_to_hidden_doc

        text = "Use List<String>, write <user@example.com>, see <https://example.com/a>."
        doc = MockDoc()
        with patch("plugin.chatbot.rich_text._insert_html_at_cursor") as mock_insert:
            ok = append_rich_text(doc, text, role="assistant")
        assert ok is True
        mock_insert.assert_not_called()
        content = doc.getText().getString()
        assert "List<String>" in content
        assert "<user@example.com>" in content
        assert "<https://example.com/a>" in content

        doc = MockDoc()
        with patch("plugin.chatbot.rich_text._insert_html_at_cursor") as mock_insert:
            render_messages_to_hidden_doc(doc, [("assistant", text)])
        mock_insert.assert_not_called()
        rendered = doc.getText().getString()
        assert "List<String>" in rendered
        assert "<user@example.com>" in rendered
        assert "<https://example.com/a>" in rendered

        doc = MockDoc()
        with patch("plugin.chatbot.rich_text._insert_html_at_cursor") as mock_insert:
            append_rich_text(doc, "<b>bold</b> and <i>italic</i> <script>x</script>", role="assistant")
        mock_insert.assert_called_once()


class TestChatTypography:
    """Tests for shared sidebar chat typography helpers."""

    def test_apply_chat_char_props(self):
        from plugin.chatbot.rich_text import (
            CHAT_FONT_HEIGHT,
            CHAT_FONT_NAME,
            CHAT_FONT_WEIGHT,
            apply_chat_char_props,
        )

        target = MagicMock()
        apply_chat_char_props(target, bg_color=0xABCDEF)
        target.CharFontName = CHAT_FONT_NAME
        target.CharHeight = CHAT_FONT_HEIGHT
        target.CharWeight = CHAT_FONT_WEIGHT
        target.CharBackColor = 0xABCDEF

    def test_apply_rich_control_para_margins(self):
        from plugin.chatbot.rich_text import CHAT_PARA_SIDE_MARGIN, apply_rich_control_para_margins

        cursor = MagicMock()
        apply_rich_control_para_margins(cursor)
        cursor.ParaLeftMargin = CHAT_PARA_SIDE_MARGIN
        cursor.ParaRightMargin = CHAT_PARA_SIDE_MARGIN
        cursor.ParaFirstLineIndent = 0

    def test_configure_hidden_writer_for_chat(self):
        from plugin.chatbot.rich_text import CHAT_FONT_NAME, configure_hidden_writer_for_chat

        std_para = MagicMock()
        para_styles = MagicMock()
        para_styles.hasByName.return_value = True
        para_styles.getByName.return_value = std_para
        style_families = MagicMock()
        style_families.hasByName.return_value = True
        style_families.getByName.return_value = para_styles
        cursor = MagicMock()
        text = MagicMock()
        text.createTextCursor.return_value = cursor
        doc = MagicMock()
        doc.getStyleFamilies.return_value = style_families
        doc.getText.return_value = text

        configure_hidden_writer_for_chat(doc)

        std_para.CharFontName = CHAT_FONT_NAME
        cursor.gotoStart.assert_called_once_with(False)
        cursor.gotoEnd.assert_called_once_with(True)


class TestChatThemeAndImporter:
    """Test suite for ChatTheme and HiddenDocHTMLImporter classes."""

    def test_chat_theme_resolution(self):
        from plugin.chatbot.rich_text import ChatTheme

        style_window = MagicMock()
        style_settings = MagicMock()
        style_settings.FieldColor = 0x1E1E1E
        style_window.StyleSettings = style_settings

        theme = ChatTheme.resolve(style_window=style_window)
        assert (theme.bg_color) == (0x1E1E1E)
        assert (theme.user_color) == (0x60A5FA)
        assert (theme.assistant_color) == (0xE2E8F0)

    def test_importer_insert_and_tighten(self):
        from plugin.chatbot.rich_text import HiddenDocHTMLImporter

        doc = MockDoc()
        importer = HiddenDocHTMLImporter(doc)
        
        cursor = MockTextCursor()
        with patch("plugin.chatbot.rich_text._insert_html_at_cursor") as mock_insert:
            importer.insert_html_at_cursor(cursor, "<p>Hi</p>")
            mock_insert.assert_called_once_with(doc, cursor, "<p>Hi</p>")

        body_range = MagicMock()
        with patch("plugin.chatbot.rich_text._tighten_list_indent") as mock_tighten:
            importer.tighten_list_indent(body_range)
            mock_tighten.assert_called_once_with(body_range)

    def test_insert_html_at_cursor_forwards_sidebar_css(self):
        from plugin.chatbot import rich_text

        cursor = MockTextCursor()
        with patch("plugin.writer.html_import.insert_html_fragment_at_cursor") as mock_insert:
            rich_text._insert_html_at_cursor(MockDoc(), cursor, "<p>Hi</p>")
        mock_insert.assert_called_once_with(
            cursor,
            "<p>Hi</p>",
            extra_css=rich_text._SIDEBAR_LIST_CSS,
        )


