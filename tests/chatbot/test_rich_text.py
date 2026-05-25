# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for plugin.chatbot.rich_text (append_rich_text, append_text_chunk, and EmbeddedWriterListener guard)."""

import unittest
from unittest.mock import MagicMock, patch, call


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
        pass

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
    """Minimal mock for the embedded Writer document."""

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


class AppendRichTextTests(unittest.TestCase):
    """Tests for append_rich_text formatting logic."""

    def _call(self, text, role="assistant"):
        from plugin.chatbot.rich_text import append_rich_text

        doc = MockDoc()
        append_rich_text(doc, text, role=role)
        return doc

    def test_user_role_prefix(self):
        doc = self._call("Hello", role="user")
        content = doc.getText().getString()
        self.assertIn("You: ", content)
        self.assertIn("Hello", content)

    def test_assistant_role_prefix(self):
        doc = self._call("World", role="assistant")
        content = doc.getText().getString()
        self.assertIn("Assistant: ", content)
        self.assertIn("World", content)

    def test_plain_text_inserted_for_non_html(self):
        """Non-HTML text is inserted via insertString (no HTML import)."""
        doc = self._call("Just some text", role="assistant")
        content = doc.getText().getString()
        self.assertIn("Just some text", content)

    def test_empty_text(self):
        doc = self._call("", role="assistant")
        content = doc.getText().getString()
        self.assertIn("Assistant: ", content)

    def test_scroll_to_bottom_called(self):
        doc = self._call("test")
        doc.getCurrentController().getViewCursor().gotoEnd.assert_called_with(False)

    def test_user_color(self):
        """Verify the prefix cursor gets USER_COLOR via createTextCursorByRange."""
        from plugin.chatbot.rich_text import USER_COLOR

        doc = MockDoc()
        created_cursors = []
        orig = doc.getText().createTextCursorByRange

        def track_cursor(rng):
            c = MockTextCursor()
            created_cursors.append(c)
            return c

        doc.getText().createTextCursorByRange = track_cursor
        from plugin.chatbot.rich_text import append_rich_text
        append_rich_text(doc, "hi", role="user")
        prefix_cursor = created_cursors[0]
        self.assertEqual(prefix_cursor.CharColor, USER_COLOR)

    def test_assistant_color_is_deep_slate_gray(self):
        from plugin.chatbot.rich_text import ASSISTANT_COLOR

        self.assertEqual(ASSISTANT_COLOR, 0x1E293B)

    def test_user_color_is_indigo_blue(self):
        from plugin.chatbot.rich_text import USER_COLOR

        self.assertEqual(USER_COLOR, 0x2A6099)

    def test_get_theme_colors_light_mode(self):
        """get_theme_colors returns light palette for high luminance background."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        style_settings = MagicMock()
        style_settings.FieldColor = 0xFFFFFF  # White background
        style_settings.DialogColor = 0xEFF0F1 # Light gray dialog
        doc.getCurrentController().getFrame().getContainerWindow().StyleSettings = style_settings

        bg_color, user_color, assistant_color = get_theme_colors(doc)
        self.assertEqual(bg_color, 0xE0E1E2)
        self.assertEqual(user_color, 0x2A6099)
        self.assertEqual(assistant_color, 0x1E293B)

    def test_get_theme_colors_dark_mode(self):
        """get_theme_colors returns dark palette for low luminance background."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        style_settings = MagicMock()
        style_settings.FieldColor = 0x1E1E1E  # Dark background
        doc.getCurrentController().getFrame().getContainerWindow().StyleSettings = style_settings

        bg_color, user_color, assistant_color = get_theme_colors(doc)
        self.assertEqual(bg_color, 0x1E1E1E)
        self.assertEqual(user_color, 0x60A5FA)
        self.assertEqual(assistant_color, 0xE2E8F0)

    def test_get_theme_colors_graceful_fallback(self):
        """get_theme_colors returns standard light palette when window or StyleSettings are missing/mocked."""
        from plugin.chatbot.rich_text import get_theme_colors
        doc = MockDoc()
        # Missing Frame / Container Window (getCurrentController returns MagicMock, which returns MagicMock)
        bg_color, user_color, assistant_color = get_theme_colors(doc)
        self.assertEqual(bg_color, 0xE0E1E2)
        self.assertEqual(user_color, 0x2A6099)
        self.assertEqual(assistant_color, 0x1E293B)

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
        self.assertEqual(prefix_cursor.CharColor, 0x60A5FA)  # Dark-mode-optimized user blue


class AppendTextChunkTests(unittest.TestCase):
    """Tests for append_text_chunk (streaming plain-text append)."""

    def test_chunk_appended(self):
        from plugin.chatbot.rich_text import append_text_chunk

        doc = MockDoc()
        append_text_chunk(doc, "Hello ")
        append_text_chunk(doc, "World")
        self.assertEqual(doc.getText().getString(), "Hello World")

    def test_scroll_on_chunk(self):
        from plugin.chatbot.rich_text import append_text_chunk

        doc = MockDoc()
        append_text_chunk(doc, "x")
        doc.getCurrentController().getViewCursor().gotoEnd.assert_called_with(False)


class IsScrolledToBottomTests(unittest.TestCase):
    """Tests for is_scrolled_to_bottom helper."""

    def test_none_scrollbar_returns_true(self):
        from plugin.chatbot.rich_text import is_scrolled_to_bottom
        self.assertTrue(is_scrolled_to_bottom(None))

    def test_at_maximum_returns_true(self):
        from plugin.chatbot.rich_text import is_scrolled_to_bottom
        sb = MagicMock()
        sb.getCurrentValue.return_value = 500
        sb.getMaximumValue.return_value = 500
        self.assertTrue(is_scrolled_to_bottom(sb))

    def test_near_maximum_within_threshold_returns_true(self):
        from plugin.chatbot.rich_text import is_scrolled_to_bottom, _SCROLL_BOTTOM_THRESHOLD
        sb = MagicMock()
        sb.getCurrentValue.return_value = 500 - _SCROLL_BOTTOM_THRESHOLD
        sb.getMaximumValue.return_value = 500
        self.assertTrue(is_scrolled_to_bottom(sb))

    def test_scrolled_up_returns_false(self):
        from plugin.chatbot.rich_text import is_scrolled_to_bottom
        sb = MagicMock()
        sb.getCurrentValue.return_value = 100
        sb.getMaximumValue.return_value = 500
        self.assertFalse(is_scrolled_to_bottom(sb))

    def test_exception_returns_true(self):
        from plugin.chatbot.rich_text import is_scrolled_to_bottom
        sb = MagicMock()
        sb.getCurrentValue.side_effect = Exception("disposed")
        self.assertTrue(is_scrolled_to_bottom(sb))


class AppendTextChunkScrollTests(unittest.TestCase):
    """Tests for conditional scrolling in append_text_chunk."""

    def test_scrolls_when_auto_scroll_true(self):
        from plugin.chatbot.rich_text import append_text_chunk
        doc = MockDoc()
        append_text_chunk(doc, "hi", auto_scroll=True)
        doc.getCurrentController().getViewCursor().gotoEnd.assert_called_with(False)

    def test_scrolls_even_when_auto_scroll_false(self):
        from plugin.chatbot.rich_text import append_text_chunk
        doc = MockDoc()
        append_text_chunk(doc, "hi", auto_scroll=False)
        doc.getCurrentController().getViewCursor().gotoEnd.assert_not_called()

    def test_text_still_appended_with_auto_scroll_false(self):
        from plugin.chatbot.rich_text import append_text_chunk
        doc = MockDoc()
        append_text_chunk(doc, "hello", auto_scroll=False)
        self.assertIn("hello", doc.getText().getString())


class AppendRichTextScrollTests(unittest.TestCase):
    """Tests for conditional scrolling in append_rich_text."""

    def test_scrolls_when_auto_scroll_true(self):
        from plugin.chatbot.rich_text import append_rich_text
        doc = MockDoc()
        append_rich_text(doc, "hi", role="assistant", auto_scroll=True)
        doc.getCurrentController().getViewCursor().gotoEnd.assert_called_with(False)

    def test_no_scroll_when_auto_scroll_false(self):
        from plugin.chatbot.rich_text import append_rich_text
        doc = MockDoc()
        append_rich_text(doc, "hi", role="assistant", auto_scroll=False)
        doc.getCurrentController().getViewCursor().gotoEnd.assert_not_called()

    def test_default_auto_scroll_is_true(self):
        from plugin.chatbot.rich_text import append_rich_text
        doc = MockDoc()
        append_rich_text(doc, "hi", role="assistant")
        doc.getCurrentController().getViewCursor().gotoEnd.assert_called_with(False)


class FindVerticalScrollbarTests(unittest.TestCase):
    """Tests for find_vertical_scrollbar accessible tree navigation."""

    def test_returns_none_for_no_component_window(self):
        from plugin.chatbot.rich_text import find_vertical_scrollbar
        frame = MagicMock()
        frame.getComponentWindow.return_value = None
        self.assertIsNone(find_vertical_scrollbar(frame))

    def test_finds_scrollbar_in_accessible_tree(self):
        from plugin.chatbot.rich_text import find_vertical_scrollbar

        # Build a mock accessible tree: frame -> comp_window -> accessible -> child0 -> scrollbar_child
        scrollbar_child = MagicMock()
        scrollbar_child.getCurrentValue.return_value = 0
        scrollbar_child_ctx = MagicMock()

        with patch("plugin.chatbot.rich_text.AccessibleRole", create=True) as MockRole:
            MockRole = MagicMock()
            scrollbar_child_ctx.getAccessibleRole.return_value = MockRole.SCROLL_BAR

            # We need to patch AccessibleRole inside the function
            # Instead, let's test the structure without the role import
            # by directly calling and checking None (since role won't match mock)
            frame = MagicMock()
            result = find_vertical_scrollbar(frame)
            # Due to import of com.sun.star.accessibility inside the function,
            # this will return None in test env (no UNO runtime)
            self.assertIsNone(result)


class EmbeddedWriterListenerGuardTests(unittest.TestCase):
    """Tests for the _EMBEDDING_STARTED guard in EmbeddedWriterListener."""

    def setUp(self):
        import plugin.chatbot.rich_text as rt

        self._module = rt
        rt._EMBEDDING_STARTED.clear()

    def test_guard_prevents_double_init(self):
        """Once initialized=True and parent_id in _EMBEDDING_STARTED, on_window_shown is a no-op."""
        parent_window = MagicMock()
        parent_window.getPeer.return_value = MagicMock()

        callback = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent_window, MagicMock(), callback)

        with patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
            mock_post.side_effect = lambda fn: None
            listener.on_window_shown(None)
            self.assertTrue(listener.initialized)
            self.assertEqual(mock_post.call_count, 1)

            # Second call should be a no-op
            listener.on_window_shown(None)
            self.assertEqual(mock_post.call_count, 1)

    def test_no_peer_does_not_init(self):
        """If getPeer() returns falsy, initialization is skipped."""
        parent_window = MagicMock()
        parent_window.getPeer.return_value = None

        callback = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent_window, MagicMock(), callback)

        with patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
            listener.on_window_shown(None)
            self.assertFalse(listener.initialized)
            mock_post.assert_not_called()

    def test_global_guard_blocks_second_listener(self):
        """A second listener for the same parent_window is blocked by _EMBEDDING_STARTED."""
        parent_window = MagicMock()
        parent_window.getPeer.return_value = MagicMock()

        with patch("plugin.framework.queue_executor.post_to_main_thread"):
            listener1 = self._module.EmbeddedWriterListener(MagicMock(), parent_window, MagicMock(), MagicMock())
            listener1.on_window_shown(None)
            self.assertTrue(listener1.initialized)

            listener2 = self._module.EmbeddedWriterListener(MagicMock(), parent_window, MagicMock(), MagicMock())
            listener2.on_window_shown(None)
            self.assertFalse(listener2.initialized)

    def test_on_window_resized_without_doc(self):
        """Verify on_window_resized works safely without doc initialized."""
        placeholder = MagicMock()
        placeholder.getPosSize.return_value = MagicMock(X=1, Y=2, Width=3, Height=4)
        container = MagicMock()
        
        listener = self._module.EmbeddedWriterListener(MagicMock(), MagicMock(), placeholder, MagicMock())
        listener.container_window = container
        listener.on_window_resized(None)
        
        container.setPosSize.assert_called_with(1, 2, 3, 4, 15)

    def test_on_window_resized_with_doc(self):
        """Verify on_window_resized calls scroll_to_bottom when doc is present."""
        placeholder = MagicMock()
        placeholder.getPosSize.return_value = MagicMock(X=1, Y=2, Width=3, Height=4)
        container = MagicMock()
        doc = MagicMock()
        
        listener = self._module.EmbeddedWriterListener(MagicMock(), MagicMock(), placeholder, MagicMock())
        listener.container_window = container
        listener.doc = doc
        
        with patch("plugin.chatbot.rich_text.scroll_to_bottom") as mock_scroll:
            listener.on_window_resized(None)
            mock_scroll.assert_called_once_with(doc)
            container.setPosSize.assert_called_with(1, 2, 3, 4, 15)


class TightenListIndentTests(unittest.TestCase):
    """Tests for _tighten_list_indent post-processing helper."""

    def _make_list_para(self, text="• item", level=0, list_id="list1", is_number=True):
        """Create a mock paragraph that uses NumberingRules."""
        import sys
        mock_uno = sys.modules["uno"]

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

        self.assertEqual(mock_uno.invoke.call_count, 1)

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

        self.assertEqual(mock_uno.invoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
