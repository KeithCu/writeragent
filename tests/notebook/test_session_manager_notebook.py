# WriterAgent - notebook session id tests

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.session_manager import (
    notebook_session_id,
    reset_notebook_python_session,
    reset_workbook_python_session,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def _writer_doc(url: str = "file:///tmp/nb.odt"):
    doc = MagicMock()
    doc.getURL.return_value = url
    return doc


def test_notebook_session_id_with_url():
    ctx = MagicMock()
    doc = _writer_doc()
    with patch("plugin.scripting.session_manager.is_writer", return_value=True):
        sid = notebook_session_id(ctx, doc)
    assert sid == "notebook:file:///tmp/nb.odt"


def test_notebook_session_id_untitled_uses_property():
    ctx = MagicMock()
    doc = _writer_doc("")
    with (
        patch("plugin.scripting.session_manager.is_writer", return_value=True),
        patch("plugin.scripting.session_manager.get_document_property", return_value="uuid-1"),
    ):
        sid = notebook_session_id(ctx, doc)
    assert sid == "notebook:uuid-1"


def test_notebook_session_id_non_writer_returns_none():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.scripting.session_manager.is_writer", return_value=False):
        assert notebook_session_id(ctx, doc) is None


def test_reset_notebook_python_session_calls_worker():
    ctx = MagicMock()
    doc = _writer_doc()
    with (
        patch("plugin.scripting.session_manager._writer_document", return_value=doc),
        patch("plugin.scripting.session_manager.has_notebook_registry", return_value=True),
        patch("plugin.scripting.session_manager.notebook_session_id", return_value="notebook:file:///tmp/nb.odt"),
        patch("plugin.scripting.session_manager.reset_python_session", return_value={"status": "ok"}) as mock_reset,
        patch("plugin.scripting.session_manager.msgbox"),
    ):
        reset_notebook_python_session(ctx)
    mock_reset.assert_called_once_with(ctx, "notebook:file:///tmp/nb.odt")


def test_reset_workbook_python_session_dispatches_to_notebook():
    ctx = MagicMock()
    doc = _writer_doc()
    with (
        patch("plugin.scripting.session_manager._active_document", return_value=doc),
        patch("plugin.scripting.session_manager.is_writer", return_value=True),
        patch("plugin.scripting.session_manager.has_notebook_registry", return_value=True),
        patch("plugin.scripting.session_manager.reset_notebook_python_session") as mock_nb_reset,
    ):
        reset_workbook_python_session(ctx)
    mock_nb_reset.assert_called_once_with(ctx)
