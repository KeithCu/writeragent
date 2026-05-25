# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for plugin.chatbot.rich_text (append_rich_text and EmbeddedWriterListener guard)."""

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

    def getStart(self):
        return self

    def gotoRange(self, target, select):
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

    def test_code_block_detected(self):
        doc = self._call("Before\n```python\nprint('hi')\n```\nAfter", role="assistant")
        content = doc.getText().getString()
        self.assertIn("print('hi')", content)
        self.assertIn("Before", content)
        self.assertIn("After", content)

    def test_empty_text(self):
        doc = self._call("", role="assistant")
        content = doc.getText().getString()
        self.assertIn("Assistant: ", content)

    def test_scroll_to_bottom_called(self):
        doc = self._call("test")
        doc.getCurrentController().select.assert_called()


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


if __name__ == "__main__":
    unittest.main()
