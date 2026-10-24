# WriterAgent - notebook session id tests

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.session_manager import (
    notebook_session_id,
    reset_notebook_python_session,
    reset_workbook_python_session,
)


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

def test_notebook_session_id_save_as_follows_url():
    ctx = MagicMock()
    doc = _writer_doc("file:///tmp/new.odt")
    with (
        patch("plugin.scripting.session_manager.is_writer", return_value=True),
        patch("plugin.scripting.session_manager.get_document_property", return_value="uuid-1"),
        patch("plugin.doc.udprops.remove_document_property") as rm,
    ):
        sid = notebook_session_id(ctx, doc)
    assert sid == "notebook:file:///tmp/new.odt"
    rm.assert_called_once()


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
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=True),
        patch("plugin.scripting.venv_worker.reset_python_session", return_value={"status": "ok"}) as mock_reset,
        patch("plugin.scripting.session_manager._msgbox"),
    ):
        reset_notebook_python_session(ctx)
    mock_reset.assert_called_once_with(ctx, "notebook:file:///tmp/nb.odt")


def test_reset_notebook_python_session_resets_execution_counter():
    ctx = MagicMock()
    doc = _writer_doc()
    state = MagicMock()
    state.next_execution_count = 9
    with (
        patch("plugin.scripting.session_manager._writer_document", return_value=doc),
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=True),
        patch("plugin.scripting.venv_worker.reset_python_session", return_value={"status": "ok"}),
        patch("plugin.scripting.session_manager._msgbox"),
        patch("plugin.notebook.cell_registry.load_registry", return_value=state) as load,
        patch("plugin.notebook.cell_registry.save_registry") as save,
    ):
        reset_notebook_python_session(ctx)
    load.assert_called_once_with(doc)
    assert state.next_execution_count == 1
    save.assert_called_once_with(doc, state)


def test_reset_notebook_python_session_resets_count_when_worker_fails():
    ctx = MagicMock()
    doc = _writer_doc()
    state = MagicMock()
    state.next_execution_count = 9
    with (
        patch("plugin.scripting.session_manager._writer_document", return_value=doc),
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=True),
        patch("plugin.scripting.venv_worker.reset_python_session", return_value={"status": "error"}),
        patch("plugin.scripting.session_manager._msgbox"),
        patch("plugin.notebook.cell_registry.load_registry", return_value=state),
        patch("plugin.notebook.cell_registry.save_registry") as save,
    ):
        reset_notebook_python_session(ctx)
    assert state.next_execution_count == 1
    save.assert_called_once_with(doc, state)


def test_reset_notebook_does_not_mint_without_existing_key():
    ctx = MagicMock()
    doc = _writer_doc("")
    state = MagicMock()
    state.next_execution_count = 5
    with (
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=True),
        patch("plugin.scripting.session_manager.get_document_property", return_value=None),
        patch("plugin.scripting.session_manager.set_document_property") as mock_set_prop,
        patch("plugin.scripting.venv_worker.reset_python_session") as mock_reset,
        patch("plugin.notebook.cell_registry.load_registry", return_value=state) as mock_load,
        patch("plugin.notebook.cell_registry.save_registry") as mock_save,
        patch("plugin.scripting.session_manager._msgbox") as mock_msgbox,
    ):
        reset_notebook_python_session(ctx, doc)

    mock_load.assert_called_once_with(doc)
    assert state.next_execution_count == 1
    mock_save.assert_called_once_with(doc, state)
    mock_set_prop.assert_not_called()
    mock_reset.assert_not_called()
    mock_msgbox.assert_called_once()
    assert "This notebook has no Python worker session yet. The execution counter was reset." in mock_msgbox.call_args[0][1]


def test_reset_workbook_python_session_dispatches_to_notebook():
    ctx = MagicMock()
    doc = _writer_doc()

    def _create_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = doc
        return enum

    comps = MagicMock()
    comps.createEnumeration.side_effect = _create_enum
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    desktop.getComponents.return_value = comps
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager.is_calc", return_value=False),
        patch("plugin.scripting.session_manager.is_writer", return_value=True),
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=True),
        patch("plugin.scripting.session_manager.reset_notebook_python_session") as mock_nb_reset,
        patch("plugin.scripting.session_manager._reset_rps_python_session") as mock_rps,
    ):
        reset_workbook_python_session(ctx)
    mock_nb_reset.assert_called_once_with(ctx, doc)
    mock_rps.assert_called_once_with(ctx, doc, notify=False)
