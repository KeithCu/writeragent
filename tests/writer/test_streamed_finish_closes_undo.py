"""Empty streamed finish() must still close the compound undo context."""

from unittest.mock import MagicMock, patch

from plugin.writer import edit_review


def _session(cls):
    undo = MagicMock()
    doc = MagicMock()
    with patch.object(edit_review, "_begin_streamed_edit", return_value=(undo, False)):
        s = cls(doc, MagicMock(), "orig")
    undo.reset_mock()
    return s, undo


def test_empty_append_finish_closes_undo():
    s, undo = _session(edit_review.WriterStreamedAppendSession)
    s.finish()
    undo.close.assert_called()


def test_empty_rewrite_finish_closes_undo():
    s, undo = _session(edit_review.WriterStreamedRewriteSession)
    s.finish()
    undo.close.assert_called()
