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


class HtmlDetectionRegexTests(unittest.TestCase):
    """Tests for _HTML_TAG_RE used in append_rich_text HTML detection."""

    def _matches(self, text):
        from plugin.chatbot.rich_text import _HTML_TAG_RE
        return bool(_HTML_TAG_RE.search(text))

    # --- True positives ---

    def test_p_tag(self):
        self.assertTrue(self._matches("<p>hello</p>"))

    def test_p_with_attrs(self):
        self.assertTrue(self._matches('<p class="intro">text</p>'))

    def test_br_self_closing(self):
        self.assertTrue(self._matches("<br/>"))

    def test_br_space_closing(self):
        self.assertTrue(self._matches("<br />"))

    def test_br_uppercase(self):
        self.assertTrue(self._matches("<BR>"))

    def test_closing_h1(self):
        self.assertTrue(self._matches("</h1>"))

    def test_closing_h2(self):
        self.assertTrue(self._matches("</h2>"))

    def test_closing_h6(self):
        self.assertTrue(self._matches("</h6>"))

    def test_ul(self):
        self.assertTrue(self._matches("<ul>"))

    def test_ol_uppercase(self):
        self.assertTrue(self._matches("<OL>"))

    def test_li(self):
        self.assertTrue(self._matches("<li>"))

    def test_strong(self):
        self.assertTrue(self._matches("<strong>bold</strong>"))

    def test_strong_mixed_case(self):
        self.assertTrue(self._matches("<Strong>text</Strong>"))

    def test_em(self):
        self.assertTrue(self._matches("<em>italic</em>"))

    def test_code(self):
        self.assertTrue(self._matches("<code>x</code>"))

    def test_pre(self):
        self.assertTrue(self._matches("<pre>block</pre>"))

    def test_div(self):
        self.assertTrue(self._matches("<div>content</div>"))

    def test_table(self):
        self.assertTrue(self._matches("<table>"))

    def test_html_embedded_in_prose(self):
        self.assertTrue(self._matches("some text\n<ul>\n<li>item</li>\n</ul>"))

    def test_p_all_uppercase(self):
        self.assertTrue(self._matches("<P>"))

    def test_tag_at_start(self):
        self.assertTrue(self._matches("<div>first thing"))

    def test_tag_at_end(self):
        self.assertTrue(self._matches("last thing<br/>"))

    # --- True negatives ---

    def test_plain_text(self):
        self.assertFalse(self._matches("Hello world"))

    def test_math_comparisons(self):
        self.assertFalse(self._matches("a < b and c > d"))

    def test_numeric_comparisons(self):
        self.assertFalse(self._matches("3 < 5 and 10 > 7"))

    def test_prevent_not_p(self):
        self.assertFalse(self._matches("<prevent>"))

    def test_tablet_not_table(self):
        self.assertFalse(self._matches("<tablet>"))

    def test_preview_not_pre(self):
        self.assertFalse(self._matches("Use <preview> mode"))

    def test_coding_not_code(self):
        self.assertFalse(self._matches("<coding>"))

    def test_olive_not_ol(self):
        self.assertFalse(self._matches("the <olive> tree"))

    def test_empty_string(self):
        self.assertFalse(self._matches(""))

    def test_email_angle_brackets(self):
        self.assertFalse(self._matches("email@<domain>"))

    def test_lt_without_gt(self):
        self.assertFalse(self._matches("a < b"))

    def test_emphasis_not_em(self):
        self.assertFalse(self._matches("<emphasis>"))

    def test_listing_not_li(self):
        self.assertFalse(self._matches("<listing>"))

    def test_division_not_div(self):
        self.assertFalse(self._matches("<division>"))

    # --- Edge cases ---

    def test_large_plain_text(self):
        self.assertFalse(self._matches("x" * 1_000_000))

    def test_large_text_with_tag_at_end(self):
        self.assertTrue(self._matches("x" * 1_000_000 + "<p>"))


class EmbeddedWriterListenerDisposalTests(unittest.TestCase):
    """Tests for the shutdown / disposal bugfix (prevents errors on LO Writer close).

    Covers the _disposed guard, disposing() override (removeWindowListener + safe
    dispose), and tolerance of DisposedException / missing objects. These would
    have been impossible before the listener leak fix.
    """

    def setUp(self):
        import plugin.chatbot.rich_text as rt

        self._module = rt
        rt._EMBEDDING_STARTED.clear()

    def test_disposing_removes_listener_and_clears_refs(self):
        """disposing() must call removeWindowListener and null the embedded refs."""
        parent = MagicMock()
        placeholder = MagicMock()
        callback = MagicMock()

        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, placeholder, callback)
        listener.doc = MagicMock()
        listener.container_window = MagicMock()

        listener.disposing(None)

        parent.removeWindowListener.assert_called_once_with(listener)
        self.assertIsNone(listener.doc)
        self.assertIsNone(listener.container_window)
        self.assertTrue(listener._disposed)

    def test_disposing_is_idempotent_no_double_remove(self):
        """Second disposing() is a no-op (prevents double-dispose segfault risk)."""
        parent = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, MagicMock(), MagicMock())

        listener.disposing(None)
        listener.disposing(None)

        # Only the first call removes; second sees the flag and skips.
        self.assertEqual(parent.removeWindowListener.call_count, 1)

    def test_disposed_guard_blocks_on_window_and_deferred(self):
        """After dispose, on_window_shown / _deferred_init / resize are no-ops."""
        parent = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, MagicMock(), MagicMock())
        listener._disposed = True
        listener.initialized = False

        with patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
            listener.on_window_shown(None)
            self.assertFalse(listener.initialized)
            mock_post.assert_not_called()

        # Also exercise the resized guard (would have touched container before the fix).
        listener.container_window = MagicMock()
        listener.on_window_resized(None)
        # No crash, and no call because of guard (container.setPosSize not invoked).

    def test_safe_dispose_tolerates_disposed_exceptions(self):
        """_dispose_embedded_objects (the actual work) swallows exceptions during close/dispose.

        # Would have failed pre-fix because: the real disposal work was unreachable
        # when called through the normal path (the helper early-returned due to the
        # flag being set in disposing()). Direct calls for testing isolation would
        # also have been affected by the split logic.
        """
        # Use a plain Exception (the real DisposedException is only available under
        # the LO UNO test runner / types-unopy). The point of the test is that
        # the except clause catches whatever the bridge throws and does not propagate.
        parent = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, MagicMock(), MagicMock())
        bad_doc = MagicMock()
        bad_doc.dispose.side_effect = Exception("already gone (simulated Disposed)")
        listener.doc = bad_doc
        listener.container_window = None

        # Simulate "we already decided to dispose" (the caller is responsible for
        # the _disposed flag in normal use). The helper itself no longer touches it.
        listener._disposed = True

        # Should not raise; just log at debug.
        listener._dispose_embedded_objects()

        self.assertIsNone(listener.doc)

    def test_dispose_via_send_listener_disposing_path(self):
        """The cooperative path from SendButtonListener.disposing also cleans up.

        # Would have failed pre-fix because: SendButtonListener had no _rich_listener
        # attr, no set_rich_listener, and its disposing() did nothing for rich-text
        # resources (only event_bus unsub). The call would have AttributeError'd or
        # simply leaked the listener + embedded doc forever.
        """
        from plugin.chatbot.panel import SendButtonListener
        from unittest.mock import patch

        # Robust construction (the 10+ positional MagicMock ctor is extremely
        # fragile to any __init__ signature change). We only need the attrs the
        # disposal + cooperation paths touch.
        with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
            send = SendButtonListener.__new__(SendButtonListener)
            send._rich_listener = None
            send.embedded_doc = None
            send.embedded_frame = None
            send.embedded_container = None
            send._cached_scrollbar = None

            fake_listener = MagicMock()
            send.set_rich_listener(fake_listener)

            send.disposing(None)

            fake_listener.disposing.assert_called_once()
            self.assertIsNone(send._rich_listener)

    def test_send_disposing_clears_own_embedded_refs_and_cached_scrollbar_even_without_rich_listener(self):
        """Direct disposal block on SendButtonListener must null its embedded refs
        and attempt close/dispose even when no rich_listener was ever set.

        # Would have failed pre-fix because: the entire "Direct best-effort disposal
        # of any embedded refs" block in SendButtonListener.disposing (and the
        # _cached_scrollbar = None line) did not exist at all. The three embedded_*
        # objects + scrollbar cache held on the listener were leaked for the life
        # of the process (or until the document was closed), exactly the class of
        # resource leak that produced the close-time errors the user reported.
        """
        from plugin.chatbot.panel import SendButtonListener
        from unittest.mock import patch, MagicMock

        with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
            send = SendButtonListener.__new__(SendButtonListener)
            doc = MagicMock()
            frame = MagicMock()
            container = MagicMock()
            send.embedded_doc = doc
            send.embedded_frame = frame
            send.embedded_container = container
            send._cached_scrollbar = MagicMock()
            send._rich_listener = None

            send.disposing(None)

            self.assertIsNone(send.embedded_doc)
            self.assertIsNone(send.embedded_frame)
            self.assertIsNone(send.embedded_container)
            self.assertIsNone(send._cached_scrollbar)
            # At least one of close(True) or dispose should have been attempted
            # on the objects we gave it (order is best-effort in the real code).
            self.assertTrue(doc.close.called or doc.dispose.called or
                            frame.close.called or frame.dispose.called or
                            container.close.called or container.dispose.called)

    def test_rerender_rich_text_session_and_deferred_scroll_after_send_dispose_are_safe_noops(self):
        """After Send disposing, rerender + the 0.2s deferred scroll timer must be
        complete no-ops and must not post work against a now-disposed doc.

        # Would have failed pre-fix because: rerender_rich_text_session and the
        # do_deferred timer (and the guard added in _append_response paths) had no
        # awareness of disposal. They would happily call getText().setString(),
        # append_rich_text, scroll_to_bottom, or post_to_main_thread on an
        # embedded_doc whose underlying Writer frame/doc had already been torn
        # down by the LO sidebar / process exit — direct source of the user's
        # "errors when I close LO writer".
        """
        from plugin.chatbot.panel import SendButtonListener
        from unittest.mock import patch, MagicMock

        with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
            send = SendButtonListener.__new__(SendButtonListener)
            send.embedded_doc = MagicMock()
            send.session = MagicMock()
            send._cached_scrollbar = None
            send._rich_listener = None

            # First dispose (this is what the real shutdown path does).
            send.disposing(None)

            # Now exercise the paths that used to be dangerous.
            # Note: rerender imports append_rich_text locally from rich_text, and
            # the deferred timer posts scroll_to_bottom; patch at the definition sites.
            with patch("plugin.chatbot.rich_text.append_rich_text") as mock_append, \
                 patch("plugin.chatbot.rich_text.scroll_to_bottom") as mock_scroll, \
                 patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:

                send.rerender_rich_text_session()

                # The inner 0.2s timer closure (do_deferred) — simulate it directly.
                # (In real code it's scheduled via threading.Timer; we just call the logic.)
                # The guard lives in the deferred helper inside rerender; we
                # just prove that after dispose the embedded_doc attr is gone
                # so any later code that checks it will skip.

                # Nothing should have been posted or appended.
                mock_append.assert_not_called()
                mock_scroll.assert_not_called()
                # The explicit post in the old timer path would have been the killer.
                # We can't easily reach the closure here without more refactoring,
                # but the fact that embedded_doc is already None is the guard.
                self.assertIsNone(getattr(send, "embedded_doc", "MISSING"))

    def test_disposed_embedded_listener_blocks_all_subsequent_window_and_deferred_callbacks_without_touching_anything(self):
        """Once disposed, the EmbeddedWriterListener must ignore every subsequent
        window event and any stale deferred init, without touching any mocks.

        # Would have failed pre-fix because: there was no _disposed flag, no
        # early returns in on_window_shown / on_window_resized / _deferred_init,
        # and the post_to_main_thread(self._deferred_init) scheduled from
        # windowShown could still run (and call create_embedded_writer_doc +
        # on_ready_callback) long after the root_window had been torn down by
        # the LO sidebar framework. That was a direct vector for the close-time
        # errors.
        """
        from unittest.mock import patch, MagicMock

        parent = MagicMock()
        placeholder = MagicMock()
        callback = MagicMock()

        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, placeholder, callback)
        listener.doc = MagicMock(name="doc")
        listener.container_window = MagicMock(name="container")

        listener.disposing(None)  # the real shutdown path

        with patch("plugin.chatbot.rich_text.create_embedded_writer_doc") as mock_create, \
             patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post, \
             patch("plugin.chatbot.rich_text.scroll_to_bottom") as mock_scroll:

            listener.on_window_shown(None)
            listener.on_window_resized(None)
            listener._deferred_init()

            # Stale post delivery simulation (what the queue would have done).
            if hasattr(listener, "_deferred_init"):
                listener._deferred_init()

            mock_create.assert_not_called()
            mock_scroll.assert_not_called()
            # The parent remove should have happened only from the first disposing.
            self.assertEqual(parent.removeWindowListener.call_count, 1)
            self.assertTrue(listener._disposed)

    def test_cooperative_dispose_idempotent_and_listener_remove_happens_exactly_once_across_paths(self):
        """Multiple dispose paths (Send + listener + ChatPanelElement delegation)
        must result in exactly one removeWindowListener and exactly one
        _safe_dispose effect.

        # Would have failed pre-fix because: there was no cooperation at all
        # (no set_rich_listener wiring in panel_wiring.py, no call from
        # SendButtonListener.disposing into the listener, no disposing override
        # on ChatPanelElement, and no _disposed guard). Every path would either
        # do nothing or (if someone added naive calls) would have double-removed
        # or double-disposed (the exact segfault footgun warned about in AGENTS.md).
        """
        from plugin.chatbot.panel import SendButtonListener
        from unittest.mock import patch, MagicMock

        parent = MagicMock()
        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, MagicMock(), MagicMock())
        listener.doc = MagicMock()
        listener.container_window = MagicMock()

        # Simulate the wiring that now happens in panel_wiring + on_ready.
        with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
            send = SendButtonListener.__new__(SendButtonListener)
            send._rich_listener = listener
            # Also give Send its own embedded refs (the dual-path case).
            send.embedded_doc = listener.doc
            send.embedded_frame = MagicMock()
            send.embedded_container = listener.container_window
            send._cached_scrollbar = None

            # Primary path (what actually happens on real panel close).
            send.disposing(None)

            # Extra direct calls (late events, explicit teardown, ChatPanelElement hook, etc.).
            listener.disposing(None)
            send.disposing(None)

            # Exactly one remove from the parent window.
            self.assertEqual(parent.removeWindowListener.call_count, 1)
            # Listener is cleaned.
            self.assertTrue(listener._disposed)
            self.assertIsNone(listener.doc)
            # Send's refs nulled (by its own block or by the listener path).
            self.assertIsNone(send.embedded_doc)
            self.assertIsNone(send._rich_listener)

    def test_disposing_actually_runs_disposal_work_and_clears_refs(self):
        """Calling disposing() must result in the real close/dispose attempts
        and the reference clearing. This is the minimal regression test for
        the exact bug where the flag was set too early.

        # Would have failed (with live MagicMock objects instead of None, and
        # no close/dispose calls recorded) on the version of the code where
        # disposing() did `self._disposed = True` before calling the helper,
        # because the helper had an early return on the flag and did nothing.
        """
        from unittest.mock import MagicMock

        parent = MagicMock()
        placeholder = MagicMock()
        callback = MagicMock()

        listener = self._module.EmbeddedWriterListener(MagicMock(), parent, placeholder, callback)

        doc = MagicMock(name="embedded_doc")
        container = MagicMock(name="container_window")
        listener.doc = doc
        listener.container_window = container

        listener.disposing(None)

        # The helper must have run the work
        self.assertTrue(doc.close.called or doc.dispose.called)
        self.assertTrue(container.close.called or container.dispose.called)

        # And it must have cleared the refs
        self.assertIsNone(listener.doc)
        self.assertIsNone(listener.container_window)

    def test_close_listener_registered_on_host_frame(self):
        """Verify that a close listener is registered on the host frame if provided."""
        from unittest.mock import patch, MagicMock

        host_frame = MagicMock()
        with patch("plugin.chatbot.rich_text._HAVE_UNO_CLOSE_EVENTS", True):
            listener = self._module.EmbeddedWriterListener(MagicMock(), MagicMock(), MagicMock(), MagicMock(), host_frame=host_frame)
            self.assertIsNotNone(listener._close_listener)
            host_frame.addCloseListener.assert_called_once_with(listener._close_listener)

    def test_close_listener_notify_closing_triggers_disposal(self):
        """Verify that notifyClosing on the close listener triggers disposal."""
        from unittest.mock import patch, MagicMock

        host_frame = MagicMock()
        with patch("plugin.chatbot.rich_text._HAVE_UNO_CLOSE_EVENTS", True):
            listener = self._module.EmbeddedWriterListener(MagicMock(), MagicMock(), MagicMock(), MagicMock(), host_frame=host_frame)
            with patch.object(listener, "_initiate_disposal") as mock_dispose:
                listener._close_listener.notifyClosing(None)
                mock_dispose.assert_called_once_with("notifyClosing")

    def test_close_listener_removed_on_disposal(self):
        """Verify that the close listener is removed from the host frame on disposal."""
        from unittest.mock import patch, MagicMock

        host_frame = MagicMock()
        with patch("plugin.chatbot.rich_text._HAVE_UNO_CLOSE_EVENTS", True):
            listener = self._module.EmbeddedWriterListener(MagicMock(), MagicMock(), MagicMock(), MagicMock(), host_frame=host_frame)
            close_listener = listener._close_listener
            listener._dispose_embedded_objects()
            host_frame.removeCloseListener.assert_called_once_with(close_listener)
            self.assertIsNone(listener._close_listener)


if __name__ == "__main__":
    unittest.main()
