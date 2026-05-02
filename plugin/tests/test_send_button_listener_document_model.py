"""Tests for SendButtonListener._get_document_model (frame-first vs getCurrentComponent)."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

# When a real `uno` bridge/types-unopy is loaded, setup_uno_mocks may skip attaching
# listener stubs; ensure imports used by panel → dialogs → listeners succeed.
_awt = sys.modules.get("com.sun.star.awt")
if _awt is not None and not hasattr(_awt, "XItemListener"):
    class _Stub:
        pass

    for _name in ("XActionListener", "XItemListener", "XTextListener", "XWindowListener"):
        if not hasattr(_awt, _name):
            setattr(_awt, _name, _Stub)

from plugin.modules.chatbot.panel import SendButtonListener


def _writer_model() -> MagicMock:
    m = MagicMock()
    m.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
    return m


def _non_document_component() -> MagicMock:
    m = MagicMock()
    m.supportsService.return_value = False
    return m


class TestSendButtonListenerDocumentModel(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = MagicMock()
        self.frame = MagicMock()
        self.send_control = MagicMock()
        self.stop_control = MagicMock()
        self.query_control = MagicMock()
        self.response_control = MagicMock()
        self.image_model_selector = MagicMock()
        self.model_selector = MagicMock()
        self.status_control = MagicMock()
        self.session = MagicMock()
        self.session.messages = [{"role": "system", "content": "test"}]

        patcher = patch.dict(sys.modules, {"plugin.main": MagicMock()}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.listener = SendButtonListener(
            self.ctx,
            self.frame,
            self.send_control,
            self.stop_control,
            self.query_control,
            self.response_control,
            self.image_model_selector,
            self.model_selector,
            self.status_control,
            self.session,
        )

    def test_prefers_frame_document_when_get_active_differs(self) -> None:
        writer = _writer_model()
        wrong = _non_document_component()
        self.frame.getController.return_value.getModel.return_value = writer
        with patch("plugin.modules.chatbot.panel.get_active_document", return_value=wrong):
            self.assertIs(self.listener._get_document_model(), writer)

    def test_falls_back_when_frame_missing(self) -> None:
        self.listener.frame = None
        writer = _writer_model()
        with patch("plugin.modules.chatbot.panel.get_active_document", return_value=writer):
            self.assertIs(self.listener._get_document_model(), writer)

    def test_falls_back_when_frame_get_model_raises(self) -> None:
        self.frame.getController.return_value.getModel.side_effect = ValueError("simulated frame failure")
        writer = _writer_model()
        with patch("plugin.modules.chatbot.panel.get_active_document", return_value=writer):
            self.assertIs(self.listener._get_document_model(), writer)

    def test_returns_none_when_no_compatible_document(self) -> None:
        bad = _non_document_component()
        self.frame.getController.return_value.getModel.return_value = bad
        with patch("plugin.modules.chatbot.panel.get_active_document", return_value=bad):
            self.assertIsNone(self.listener._get_document_model())


if __name__ == "__main__":
    unittest.main()
