# WriterAgent - tests for notebook run button wiring

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.notebook.notebook_controls import (
    NotebookRunButtonListener,
    get_control_view_for_model,
    wire_run_button_listener,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_get_control_view_uses_gettypebyname_for_xcontrolaccess():
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    controller.getControl.return_value = None
    model = MagicMock()
    view = MagicMock()
    access = MagicMock()
    access.getControl.return_value = view

    type_mock = MagicMock()
    with patch("plugin.notebook.notebook_controls.uno.getTypeByName", return_value=type_mock) as get_type:
        controller.queryInterface.return_value = access
        result = get_control_view_for_model(doc, model)
    assert result is view
    get_type.assert_called_with("com.sun.star.view.XControlAccess")
    controller.queryInterface.assert_called_once_with(type_mock)
    access.getControl.assert_called_once_with(model)


def test_wire_run_button_listener_attaches_to_xbutton():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/nb.odt"
    model = MagicMock()
    model.Name = "nb_run_abc"

    control = MagicMock()
    control.queryInterface.return_value = control

    with patch(
        "plugin.notebook.notebook_controls.get_control_view_for_model",
        return_value=control,
    ):
        ok = wire_run_button_listener(ctx, doc, model, "abc")
    assert ok is True
    control.addActionListener.assert_called_once()


def test_wire_run_button_listener_idempotent_for_same_runtime_uid():
    """Untitled docs share RuntimeUID across PyUNO wrappers; one click must be one run."""
    ctx = MagicMock()
    doc1 = MagicMock()
    doc2 = MagicMock()
    doc1.getURL.return_value = ""
    doc2.getURL.return_value = ""
    doc1.getRuntimeUID.return_value = "uid-nb-same"
    doc2.getRuntimeUID.return_value = "uid-nb-same"
    model = MagicMock()
    control = MagicMock()
    control.queryInterface.return_value = control
    hex_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    with patch(
        "plugin.notebook.notebook_controls.get_control_view_for_model",
        return_value=control,
    ):
        assert wire_run_button_listener(ctx, doc1, model, hex_id) is True
        assert wire_run_button_listener(ctx, doc2, model, hex_id) is True
    control.addActionListener.assert_called_once()


def test_notebook_run_button_listener_calls_runner():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookRunButtonListener(ctx, doc, "deadbeef")
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(MagicMock())
    run.assert_called_once_with(ctx, doc, "deadbeef")
