# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script Monaco integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting import python_runner as pr
from plugin.scripting import python_runner_ui as ui


def _patch_modal_native():
    return patch.object(ui, "native_run_script_modeless_enabled", return_value=False)


def _patch_modeless_native():
    return patch.object(ui, "native_run_script_modeless_enabled", return_value=True)


def test_native_run_script_modeless_enabled_reads_config():
    ctx = MagicMock()
    with patch.object(ui, "get_config", return_value=True):
        assert ui.native_run_script_modeless_enabled(ctx) is True
    with patch.object(ui, "get_config", return_value=False):
        assert ui.native_run_script_modeless_enabled(ctx) is False


def test_run_python_dialog_uses_monaco_when_available():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "is_writer", return_value=True):
                with patch.object(pr, "is_calc", return_value=False):
                    with patch.object(pr, "is_draw", return_value=False):
                        with patch.object(pr, "get_config_str", return_value="print('hi')"):
                            with patch.object(pr, "monaco_open_expected", return_value=("/venv/bin/python", True)):
                                with patch.object(pr, "_run_python_monaco", return_value=True) as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                                        pr.run_python_dialog()

    mock_monaco.assert_called_once()
    assert mock_monaco.call_args.kwargs["initial_code"].startswith("# Calculate primes")
    mock_native.assert_not_called()


def test_run_python_dialog_falls_back_to_native_dialog():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "is_writer", return_value=True):
                with patch.object(pr, "is_calc", return_value=False):
                    with patch.object(pr, "is_draw", return_value=False):
                        with patch.object(pr, "get_config_str", return_value="x = 1"):
                            with patch.object(pr, "monaco_open_expected", return_value=(None, False)):
                                with patch.object(pr, "_run_python_monaco") as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                                        with patch.object(pr, "set_config") as mock_set:
                                            with patch.object(pr, "execute_and_insert_result") as mock_execute:
                                                pr.run_python_dialog()

    mock_monaco.assert_not_called()
    mock_native.assert_called_once()
    mock_set.assert_not_called()
    mock_execute.assert_not_called()


def test_run_python_dialog_no_msgbox_when_monaco_succeeds():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "monaco_open_expected", return_value=("/venv/bin/python", True)):
                with patch.object(pr, "_run_python_monaco", return_value=True):
                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                        with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                            pr.run_python_dialog()

    mock_native.assert_not_called()
    mock_report.assert_not_called()


def test_run_python_dialog_msgbox_when_monaco_and_native_fail():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=("/venv/bin/python", True)):
                    with patch.object(pr, "_run_python_monaco", return_value=False):
                        with patch.object(pr, "show_python_input_dialog", return_value=(False, None)):
                            with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                                pr.run_python_dialog()

    mock_report.assert_not_called()


def test_run_python_dialog_msgbox_when_native_only_path_fails():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=(None, False)):
                    with patch.object(pr, "show_python_input_dialog", return_value=(False, "dialog load failed")):
                        with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                            pr.run_python_dialog()

    mock_report.assert_called_once()
    assert "built-in script dialog" in mock_report.call_args.args[1]
    assert mock_report.call_args.kwargs.get("detail") == "dialog load failed"


def test_run_python_dialog_native_failure_detail_in_msgbox():
    ctx = MagicMock()
    doc = MagicMock()
    native_detail = "Traceback (most recent call last):\n  XDL load failed"

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=(None, False)):
                    with patch.object(pr, "show_python_input_dialog", return_value=(False, native_detail)):
                        with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                            pr.run_python_dialog()

    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs.get("detail") == native_detail


def test_show_python_input_dialog_returns_false_when_xdl_missing():
    ctx = MagicMock()

    with patch.object(ui, "native_run_script_modeless_enabled", return_value=False):
        with patch.object(ui, "load_writeragent_dialog_detail", return_value=(None, "PythonScriptDialog could not be loaded from the extension.")):
            assert ui.show_python_input_dialog(ctx, "x = 1", "last_python_script_writer") == (
                False,
                "PythonScriptDialog could not be loaded from the extension.",
            )


def test_run_python_dialog_msgbox_includes_monaco_exception_when_native_fails():
    ctx = MagicMock()
    doc = MagicMock()
    monaco_exc = ImportError("No module named 'plugin.framework.prompts'")

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=("/venv/bin/python", True)):
                    with patch.object(pr, "_run_python_monaco", side_effect=monaco_exc):
                        with patch.object(pr, "show_python_input_dialog", return_value=(False, None)):
                            with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                                pr.run_python_dialog()

    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs.get("exc") is monaco_exc
    assert "Monaco editor" in mock_report.call_args.args[1]


def test_run_python_dialog_immediate_msgbox_on_monaco_exception_then_native():
    ctx = MagicMock()
    doc = MagicMock()
    monaco_exc = ImportError("broken monaco path")
    call_order: list[str] = []

    def report(*args, **kwargs):
        call_order.append("report")

    def native(*args, **kwargs):
        call_order.append("native")
        return True, None

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=("/venv/bin/python", True)):
                    with patch.object(pr, "_run_python_monaco", side_effect=monaco_exc):
                        with patch.object(pr, "show_python_input_dialog", side_effect=native):
                            with patch.object(pr, "_report_run_python_open_failed", side_effect=report):
                                pr.run_python_dialog()

    assert call_order == ["report", "native"]


def test_run_python_dialog_no_msgbox_when_monaco_not_expected_and_native_opens():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch("plugin.scripting.document_scripts.resolve_run_script_selection", return_value=("script", "x=1", {})):
                with patch.object(pr, "monaco_open_expected", return_value=(None, False)):
                    with patch.object(pr, "_run_python_monaco") as mock_monaco:
                        with patch.object(pr, "show_python_input_dialog", return_value=(True, None)):
                            with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                                pr.run_python_dialog()

    mock_monaco.assert_not_called()
    mock_report.assert_not_called()


def test_run_python_monaco_writer_skips_calc_selection_import():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "is_calc", return_value=False):
        with patch("plugin.calc.analysis_runner.calc_selection_to_a1") as mock_sel:
            with patch.object(pr, "launch_monaco_editor", return_value=True):
                with patch.object(pr, "terminate_persistent_editor"):
                    ok = pr._run_python_monaco(
                        ctx,
                        doc,
                        initial_code="# wa_vision extract_text",
                        selected_script_name="[Vision] extract_text",
                        exe="/venv/bin/python",
                    )

    assert ok is True
    mock_sel.assert_not_called()


def test_run_python_monaco_on_save_persists_and_executes():
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["exe"] = exe
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch.object(pr, "set_config") as mock_set:
            with patch.object(pr, "execute_and_insert_result", return_value={"ok": True, "status_ok_text": "done"}):
                with patch.object(pr, "get_config_str", return_value="Prime Numbers"):
                    with patch("plugin.framework.config.get_config", return_value={"Prime Numbers": "print(1)"}):
                        ok = pr._run_python_monaco(
                            ctx,
                            doc,
                            initial_code="result = 1",
                            selected_script_name="Prime Numbers",
                            exe="/venv/bin/python",
                        )

                        assert ok is True
                        load = captured["load_message"]
                        assert load["mode"] == "run_script"
                        assert load["selected_script_name"] == "Prime Numbers"
                        assert load["run_label"] is not None
                        assert load["save_label"] is not None
                        assert load["close_label"] is not None
                        assert load["show_plain_text"] is False
                        assert load["show_data_binding"] is False
                        from plugin.scripting.editor_ui_strings import enrich_monaco_load_message

                        enriched = enrich_monaco_load_message(load)
                        assert enriched["ui"]["script_label"]

                        response = captured["on_save"]("result = 2", False, None, "run")
                        mock_set.assert_called_with("saved_python_scripts", {"Prime Numbers": "result = 2"})
                        assert response == {"type": "saved", "ok": True, "status_ok_text": "done"}

                        save_response = captured["on_save"]("result = 3", False, None, "save")
                        assert save_response == {"type": "saved", "ok": True, "status_ok_text": "Script saved."}


def test_execute_and_insert_result_returns_error_on_failure():
    ctx = MagicMock()
    with patch.object(pr, "run_code_in_user_venv", return_value={"status": "error", "message": "boom"}):
        outcome = pr.execute_and_insert_result(ctx, MagicMock(), "bad()")
    assert outcome["ok"] is False
    assert outcome["message"].startswith("boom")


def test_show_python_input_dialog_run_button_keeps_dialog_open():
    ctx = MagicMock()
    desktop = MagicMock()
    frame = MagicMock()
    parent_window = MagicMock()
    desktop.getCurrentFrame.return_value = frame
    frame.getContainerWindow.return_value = parent_window

    smgr = MagicMock()
    dlg_model = MagicMock()
    dlg = MagicMock()
    toolkit = MagicMock()

    ctx.getServiceManager.return_value = smgr

    def fake_create(service, _ctx):
        if "UnoControlDialogModel" in service:
            return dlg_model
        if "UnoControlDialog" in service:
            return dlg
        if "Toolkit" in service:
            return toolkit
        return MagicMock()

    smgr.createInstanceWithContext.side_effect = fake_create

    code_edit_model = MagicMock()
    code_edit_model.Text = "result = 42"
    code_edit = MagicMock()
    code_edit.getModel.return_value = code_edit_model

    instruction_lbl_model = MagicMock()
    instruction_lbl = MagicMock()
    instruction_lbl.getModel.return_value = instruction_lbl_model

    script_select = MagicMock()
    script_select.getSelectedItemPos.return_value = 0
    script_select.getItems.return_value = ["Sample"]

    btn_run = MagicMock()
    listeners = []
    btn_run.addActionListener.side_effect = lambda listener: listeners.append(listener)

    def fake_get_control(name):
        if name == "BtnRun":
            return btn_run
        if name == "ScriptSelect":
            return script_select
        if name == "InstructionLbl":
            return instruction_lbl
        return code_edit

    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "set_config") as mock_set:
                with patch.object(ui, "get_config", return_value={"Sample": "result = 42"}) as mock_get:
                    with patch.object(ui, "get_config_str", return_value="") as mock_get_str:
                        with patch("plugin.scripting.python_runner.execute_and_insert_result", return_value={"ok": True, "status_ok_text": "done"}) as mock_execute:
                            def fake_execute_dialog():
                                for listener in listeners:
                                    if "RunListener" in type(listener).__name__:
                                        listener.actionPerformed(MagicMock())

                            dlg.execute.side_effect = fake_execute_dialog

                            ui.show_python_input_dialog(ctx, "result = 1", "last_python_script_writer")

                            dlg.endDialog.assert_not_called()
                            dlg.setVisible.assert_not_called()
                            mock_set.assert_any_call("last_python_script_name_writer", "Sample")
                            mock_set.assert_any_call("saved_python_scripts", {"Sample": "result = 42"})
                            mock_execute.assert_called_once_with(ctx, None, "result = 42")


def test_show_python_input_dialog_save_button():
    ctx = MagicMock()
    desktop = MagicMock()
    frame = MagicMock()
    parent_window = MagicMock()
    desktop.getCurrentFrame.return_value = frame
    frame.getContainerWindow.return_value = parent_window

    smgr = MagicMock()
    dlg_model = MagicMock()
    dlg = MagicMock()
    toolkit = MagicMock()

    ctx.getServiceManager.return_value = smgr
    
    # Track calls to createInstanceWithContext to return our mocks
    def fake_create(service, _ctx):
        if "UnoControlDialogModel" in service:
            return dlg_model
        if "UnoControlDialog" in service:
            return dlg
        if "Toolkit" in service:
            return toolkit
        return MagicMock()
    smgr.createInstanceWithContext.side_effect = fake_create

    # Mock elements in the dialog
    code_edit_model = MagicMock()
    code_edit_model.Text = "print('hello world')"
    code_edit = MagicMock()
    code_edit.getModel.return_value = code_edit_model
    dlg.getControl.return_value = code_edit

    script_select = MagicMock()
    script_select.getSelectedItemPos.return_value = 0
    script_select.getItems.return_value = ["Sample"]

    btn_save = MagicMock()
    listeners = []
    btn_save.addActionListener.side_effect = lambda l: listeners.append(l)
    
    def fake_get_control(name):
        if name == "BtnSave":
            return btn_save
        if name == "ScriptSelect":
            return script_select
        return code_edit
    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "set_config") as mock_set:
                with patch.object(ui, "get_config", return_value={"Sample": "print('hello')"}) as mock_get:
                    with patch.object(ui, "get_config_str", return_value="") as mock_get_str:
                        def fake_execute():
                            for listener in listeners:
                                if "SaveListener" in type(listener).__name__:
                                    listener.actionPerformed(MagicMock())
                        dlg.execute.side_effect = fake_execute

                        ui.show_python_input_dialog(ctx, "print('hello')", "last_python_script_writer")

                        mock_set.assert_any_call("saved_python_scripts", {"Sample": "print('hello world')"})


def test_persistent_editor_dispatches_script_actions():
    from plugin.scripting.editor_host import PersistentEditor
    from plugin.scripting.document_scripts import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER

    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.send = MagicMock()
    doc = MagicMock()

    with patch("plugin.framework.config.get_config", return_value={"MyScript": "print(123)"}) as mock_get:
        with patch("plugin.framework.config.get_config_str", return_value="MyScript"):
            with patch("plugin.framework.config.set_config") as mock_set:
                with patch("plugin.scripting.document_scripts.get_active_document_for_scripts", return_value=None):
                    pe._dispatch_incoming({"type": "request_scripts"})
                    mock_get.assert_any_call("saved_python_scripts")
                    sent = pe.send.call_args[0][0]
                    assert sent["type"] == "scripts_list"
                    assert sent["sections"][0]["scripts"] == {"MyScript": "print(123)"}
                    assert sent["sample_code"] == "print(123)"
                    assert sent["selected_script_name"] == "MyScript"

                pe._dispatch_incoming({"type": "select_script", "name": "MyScript"})
                mock_set.assert_called_with("last_python_script_name_writer", "MyScript")

                pe._dispatch_incoming({"type": "save_script", "name": "NewScript", "code": "x = 1", "origin": SCRIPT_ORIGIN_USER})
                mock_set.assert_called_with("saved_python_scripts", {"MyScript": "print(123)", "NewScript": "x = 1"})

                pe._dispatch_incoming({"type": "delete_script", "name": "MyScript", "origin": SCRIPT_ORIGIN_USER})
                mock_set.assert_called_with("saved_python_scripts", {"NewScript": "x = 1"})

    with patch("plugin.scripting.document_scripts.get_active_document_for_scripts", return_value=doc):
        with patch("plugin.scripting.document_scripts.save_document_script", return_value=None) as mock_save_doc:
            pe.set_run_script_document(doc)
            pe._dispatch_incoming({"type": "save_script", "name": "DocScript", "code": "y=2", "origin": SCRIPT_ORIGIN_DOCUMENT})
            mock_save_doc.assert_called_once_with(doc, "DocScript", "y=2")

    with patch("plugin.scripting.document_scripts.get_active_document_for_scripts", return_value=doc):
        with patch("plugin.scripting.document_scripts.attach_document_script", return_value=None) as mock_attach:
            pe._dispatch_incoming({"type": "attach_script", "name": "Attached", "code": "z=3", "overwrite": True})
            mock_attach.assert_called_once_with(doc, "Attached", "z=3", overwrite=True)


def test_show_python_input_dialog_save_as_button():
    ctx = MagicMock()
    desktop = MagicMock()
    frame = MagicMock()
    parent_window = MagicMock()
    desktop.getCurrentFrame.return_value = frame
    frame.getContainerWindow.return_value = parent_window

    smgr = MagicMock()
    dlg_model = MagicMock()
    dlg = MagicMock()
    toolkit = MagicMock()

    ctx.getServiceManager.return_value = smgr
    
    def fake_create(service, _ctx):
        if "UnoControlDialogModel" in service:
            return dlg_model
        if "UnoControlDialog" in service:
            return dlg
        if "Toolkit" in service:
            return toolkit
        return MagicMock()
    smgr.createInstanceWithContext.side_effect = fake_create

    # Mock elements in the dialog
    code_edit_model = MagicMock()
    code_edit_model.Text = "print('hello world')"
    code_edit = MagicMock()
    code_edit.getModel.return_value = code_edit_model
    
    script_select = MagicMock()
    script_select.getSelectedItemPos.return_value = 0
    script_select.getItems.return_value = ["Sample"]

    btn_save_as = MagicMock()
    listeners = []
    btn_save_as.addActionListener.side_effect = lambda l: listeners.append(l)
    
    def fake_get_control(name):
        if name == "ScriptSelect":
            return script_select
        if name == "BtnSaveAs":
            return btn_save_as
        return code_edit
    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "get_config", return_value={}):
                with patch.object(ui, "get_config_str", return_value=""):
                    with patch.object(ui, "set_config") as mock_set:
                        with patch.object(ui, "show_text_input_dialog", return_value="scriptk") as mock_input:
                            def fake_execute():
                                for listener in listeners:
                                    if "SaveAsListener" in type(listener).__name__:
                                        listener.actionPerformed(MagicMock())
                            dlg.execute.side_effect = fake_execute

                            ui.show_python_input_dialog(ctx, "print('hello')", "last_python_script_writer")

                            mock_input.assert_called_once()
                            mock_set.assert_any_call("saved_python_scripts", {"scriptk": "print('hello world')"})


def test_show_python_input_dialog_modeless_uses_set_visible():
    ctx = MagicMock()
    desktop = MagicMock()
    frame = MagicMock()
    parent_window = MagicMock()
    desktop.getCurrentFrame.return_value = frame
    frame.getContainerWindow.return_value = parent_window

    smgr = MagicMock()
    dlg_model = MagicMock()
    dlg = MagicMock()
    toolkit = MagicMock()
    ctx.getServiceManager.return_value = smgr

    def fake_create(service, _ctx):
        if "UnoControlDialogModel" in service:
            return dlg_model
        if "UnoControlDialog" in service:
            return dlg
        if "Toolkit" in service:
            return toolkit
        return MagicMock()

    smgr.createInstanceWithContext.side_effect = fake_create
    code_edit = MagicMock()
    script_select = MagicMock()
    dlg.getControl.side_effect = lambda name: code_edit if name == "CodeEdit" else script_select

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modeless_native():
            with patch.object(ui, "get_config", return_value={}):
                with patch.object(ui, "get_config_str", return_value=""):
                    with patch.object(ui, "set_config"):
                        ui.show_python_input_dialog(ctx, "x = 1", "last_python_script_writer")

    dlg.execute.assert_not_called()
    dlg.setVisible.assert_called_once_with(True)
    dlg.addTopWindowListener.assert_called_once()


def test_picker_select_name_combobox_uses_set_text():
    ctrl = MagicMock(spec=[])
    ctrl.setText = MagicMock()
    ui._picker_select_name(ctrl, "MyScript", ["Sample", "MyScript"])
    ctrl.setText.assert_called_once_with("MyScript")


def test_picker_selected_name_combobox_uses_get_text():
    ctrl = MagicMock(spec=[])
    ctrl.getText = MagicMock(return_value="  Prime Numbers  ")
    assert ui._picker_selected_name(ctrl) == "Prime Numbers"


def test_picker_select_name_listbox_uses_select_item_pos():
    ctrl = MagicMock()
    ui._picker_select_name(ctrl, "B", ["A", "B"])
    ctrl.selectItemPos.assert_called_once_with(1, True)


def test_picker_selected_name_listbox_uses_item_pos():
    ctrl = MagicMock()
    ctrl.getItems.return_value = ["A", "B"]
    ctrl.getSelectedItemPos.return_value = 1
    assert ui._picker_selected_name(ctrl) == "B"


def test_monaco_editor_available_respects_force_internal():
    from plugin.scripting.editor_host import monaco_editor_available
    ctx = MagicMock()
    
    with patch("plugin.framework.config.get_config", return_value=True) as mock_get:
        exe, ok = monaco_editor_available(ctx)
        assert exe is None
        assert ok is False
        mock_get.assert_called_with("scripting.force_internal_script_editor")
 
    with patch("plugin.framework.config.get_config", return_value=False):
        with patch("plugin.scripting.editor_host.resolve_editor_python", return_value=("/fake/python", None)):
            with patch("plugin.scripting.editor_host.probe_webview_import", return_value=(True, "")):
                exe, ok = monaco_editor_available(ctx)
                assert exe == "/fake/python"
                assert ok is True
