# WriterAgent - tests for notebook run button wiring

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.notebook.notebook_controls as notebook_controls
from plugin.notebook.notebook_controls import (
    NotebookFormRunListener,
    NotebookRunButtonListener,
    get_control_view_for_model,
    wire_all_notebook_run_buttons,
    wire_run_button_listener,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def setup_function() -> None:
    notebook_controls._listener_refs = []
    notebook_controls._wired_keys = set()
    notebook_controls._wired_form_docs = set()


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


def test_notebook_run_button_listener_untitled_resolves_by_runtime_uid():
    """Hidden / non-current untitled docs are not getCurrentComponent; URL is empty."""
    ctx = MagicMock()
    doc = MagicMock()
    found = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-hidden-nb"
    listener = NotebookRunButtonListener(ctx, doc, "cafebabecafebabecafebabecafebabe")
    listener._doc_weak = None
    with (
        patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(found, "writer")) as resolve,
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
        patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run,
    ):
        listener.on_action_performed(MagicMock())
    resolve.assert_called_with(ctx, "uid-hidden-nb")
    run.assert_called_once_with(ctx, found, "cafebabecafebabecafebabecafebabe")


def test_form_run_listener_dispatches_nb_run_name():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookFormRunListener(ctx, doc)
    model = MagicMock()
    model.Name = "nb_run_deadbeefdeadbeefdeadbeefdeadbeef"
    control = MagicMock()
    control.getModel.return_value = model
    ev = MagicMock()
    ev.Source = control
    ev.ActionCommand = ""
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(ev)
    run.assert_called_once_with(ctx, doc, "deadbeefdeadbeefdeadbeefdeadbeef")


def test_form_run_listener_ignores_non_run_controls():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookFormRunListener(ctx, doc)
    model = MagicMock()
    model.Name = "nb_cell_0_code"
    control = MagicMock()
    control.getModel.return_value = model
    ev = MagicMock()
    ev.Source = control
    ev.ActionCommand = ""
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(ev)
    run.assert_not_called()


def test_wire_all_attaches_one_form_listener_without_getcontrol():
    """Import wiring must not call getControl once per ▶ button."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-form-1"

    run_ctrl = MagicMock()
    run_model = MagicMock()
    run_model.Name = "nb_run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    run_ctrl.getModel.return_value = run_model
    run_ctrl.queryInterface.return_value = run_ctrl

    code_ctrl = MagicMock()
    code_model = MagicMock()
    code_model.Name = "nb_cell_0_code"
    code_ctrl.getModel.return_value = code_model
    code_ctrl.queryInterface.return_value = None
    code_ctrl.addActionListener.side_effect = RuntimeError("not a button")

    container = MagicMock()
    container.getControls.return_value = (code_ctrl, run_ctrl)
    fc = MagicMock()
    fc.getContainer.return_value = container
    controller = MagicMock()
    controller.getFormController.return_value = fc
    doc.getCurrentController.return_value = controller
    forms = MagicMock()
    forms.getCount.return_value = 1
    forms.getByIndex.return_value = MagicMock()
    doc.getDrawPage.return_value.getForms.return_value = forms

    cell = MagicMock()
    cell.cell_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    state = MagicMock()
    state.code_cells = [cell, cell]

    with (
        patch("plugin.notebook.notebook_controls.has_notebook_registry", return_value=True),
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls.get_control_view_for_model") as get_view,
        patch("plugin.notebook.writer_importer.flush_ui_idle"),
    ):
        first = wire_all_notebook_run_buttons(ctx, doc)
        second = wire_all_notebook_run_buttons(ctx, doc)
    assert first == 1
    assert second == 1
    get_view.assert_not_called()
    run_ctrl.addActionListener.assert_called_once()
    container.addContainerListener.assert_called_once()
    form_lis = [lis for lis in notebook_controls._listener_refs if getattr(lis, "_form_level", False)]
    assert len(form_lis) == 1
