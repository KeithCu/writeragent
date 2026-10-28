# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script Monaco integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting import python_runner as pr
from plugin.scripting import python_runner_ui as ui
import pytest


def _patch_modal_native():
    return patch.object(ui, "native_run_script_modeless_enabled", return_value=False)


def _patch_modeless_native():
    return patch.object(ui, "native_run_script_modeless_enabled", return_value=True)


def test_native_run_script_modeless_enabled_reads_config():
    with patch.object(ui, "get_config", return_value=True) as mock_get:
        assert ui.native_run_script_modeless_enabled() is True
        mock_get.assert_called_once_with("scripting.native_run_script_modeless")
    with patch.object(ui, "get_config", return_value=False) as mock_get:
        assert ui.native_run_script_modeless_enabled() is False
        mock_get.assert_called_once_with("scripting.native_run_script_modeless")


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
                            with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                                with patch.object(pr, "_run_python_monaco", return_value=True) as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                                        pr.run_python_dialog()

    mock_monaco.assert_called_once()
    assert "import writeragent as wa" in mock_monaco.call_args.kwargs["initial_code"]
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
                            with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
                                with patch.object(pr, "_run_python_monaco") as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                                        with patch("plugin.scripting.document_scripts.save_user_script") as mock_save:
                                            with patch.object(pr, "execute_and_insert_result") as mock_execute:
                                                pr.run_python_dialog()

    mock_monaco.assert_not_called()
    mock_native.assert_called_once()
    mock_save.assert_not_called()
    mock_execute.assert_not_called()


def test_run_python_dialog_no_msgbox_when_monaco_succeeds():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                with patch.object(pr, "_run_python_monaco", return_value=True):
                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                        with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                            pr.run_python_dialog()

    mock_native.assert_not_called()
    mock_report.assert_not_called()


def test_run_python_dialog_cancel_keeps_dirty_cell_and_skips_native():
    """A dirty cell plus Cancel must not open the native script dialog."""
    from plugin.scripting import editor_host as launch_mod

    ctx = MagicMock()
    doc = MagicMock()
    pe = launch_mod._PERSISTENT_EDITOR
    launch_mod.set_active_session(None)
    pe.sessions.clear()
    pe.focused_id = None
    pe._proc = None
    pe.run_script_doc = None
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState(
        "sid-cell",
        "calc_cell",
        {"cell_address": "C3"},
        on_save=MagicMock(),
    )
    state.dirty = True
    pe.register_session(state)
    try:
        with patch.object(pr, "get_ctx", return_value=ctx):
            with patch.object(pr, "get_desktop") as mock_desktop:
                mock_desktop.return_value.getCurrentComponent.return_value = doc
                with patch.object(pr, "is_calc", return_value=False):
                    with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                        with (
                            patch(
                                "plugin.scripting.python_runner.resolve_run_script_selection",
                                return_value=("Demo", "print(1)", {}),
                            ),
                            patch(
                                "plugin.scripting.document_scripts.build_xdl_script_picker_state",
                                return_value=([], {}, {}),
                            ),
                            patch(
                                "plugin.scripting.python_runner.build_xdl_script_picker_state",
                                return_value=([], {}, {}),
                            ),
                        ):
                            with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="cancel") as confirm:
                                with patch.object(pr, "show_python_input_dialog") as mock_native:
                                    pr.run_python_dialog()
        confirm.assert_called_once()
        assert "C3" in confirm.call_args.args[1]
        mock_native.assert_not_called()
        assert pe.focused() is state
        assert state.dirty is True
    finally:
        launch_mod.set_active_session(None)
        pe.sessions.clear()
        pe.focused_id = None
        pe._proc = None
        pe.run_script_doc = None
        if "send" in pe.__dict__:
            del pe.send


def test_run_python_dialog_msgbox_when_monaco_and_native_fail():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
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
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
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
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
                    with patch.object(pr, "show_python_input_dialog", return_value=(False, native_detail)):
                        with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                            pr.run_python_dialog()

    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs.get("detail") == native_detail


def test_run_python_dialog_reports_setup_error_when_monaco_not_expected():
    ctx = MagicMock()
    err = RuntimeError("no desktop")

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
            with patch.object(pr, "get_desktop", side_effect=err):
                with patch.object(pr, "show_python_input_dialog") as mock_native:
                    with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                        pr.run_python_dialog()

    mock_report.assert_called_once()
    assert "unexpected error" in mock_report.call_args.args[1].lower()
    assert mock_report.call_args.kwargs.get("exc") is err
    mock_native.assert_not_called()


def test_show_python_input_dialog_returns_false_when_xdl_missing():
    ctx = MagicMock()

    with patch.object(ui, "native_run_script_modeless_enabled", return_value=False):
        with patch.object(ui, "load_writeragent_dialog_detail", return_value=(None, "PythonScriptDialog could not be loaded from the extension.")):
            assert ui.show_python_input_dialog(ctx) == (
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
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                    with patch.object(pr, "_run_python_monaco", side_effect=monaco_exc):
                        with patch.object(pr, "show_python_input_dialog", return_value=(False, None)):
                            with patch.object(pr, "_report_run_python_open_failed") as mock_report:
                                pr.run_python_dialog()

    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs.get("exc") is monaco_exc
    assert "open the editor" in mock_report.call_args.args[1]


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
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
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
            with (
                patch("plugin.scripting.python_runner.resolve_run_script_selection", return_value=("script", "x=1", {})),
                patch("plugin.scripting.document_scripts.build_xdl_script_picker_state", return_value=([], {}, {})),
                patch("plugin.scripting.python_runner.build_xdl_script_picker_state", return_value=([], {}, {})),
            ):
                with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
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
        with patch("plugin.calc.bridge.calc_selection_to_a1") as mock_sel:
            with patch.object(pr, "launch_monaco_editor", return_value=True):
                ok = pr._run_python_monaco(
                    ctx,
                    doc,
                    initial_code="# wa_vision extract_text",
                    selected_script_name="[Vision] extract_text",
                    exe="/venv/bin/python",
                )

    assert ok is True
    mock_sel.assert_not_called()


def _complete_monaco_run(result, outcome):
    """Start a deferred Monaco Run and return the frame delivered to the host."""
    from plugin.scripting.editor_host import DeferredEditorResult

    assert isinstance(result, DeferredEditorResult)
    delivered: list[dict] = []

    def _side(*_args, **kwargs):
        kwargs["on_complete"](outcome)

    with patch("plugin.scripting.python_runner.start_native_script_run", side_effect=_side) as mock_run:
        result.start(delivered.append)
    assert delivered
    return delivered[0], mock_run


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
        with patch("plugin.scripting.document_scripts.save_user_script") as mock_save:
            with patch.object(pr, "execute_and_insert_result") as mock_execute:
                with patch.object(pr, "get_config_str", return_value="Prime Numbers"):
                    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"Prime Numbers": "print(1)"}):
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
                        assert enriched["save_as_plain"] is False
                        assert enriched["ui"]["script_label"]

                        response = captured["on_save"]("result = 2", False, "A1:B2", "run")
                        mock_save.assert_called_with("Prime Numbers", "result = 2")
                        mock_execute.assert_not_called()
                        busy = captured["on_save"]("result = 9", False, None, "run")
                        assert busy["type"] == "error"
                        assert "already running" in busy["message"]
                        # The refused Run must not write the library or start another venv.
                        mock_save.assert_called_once_with("Prime Numbers", "result = 2")

                        save_response = captured["on_save"]("result = 3", False, None, "save")
                        assert save_response == {"type": "saved", "ok": True, "status_ok_text": "Script saved."}
                        mock_save.assert_called_with("Prime Numbers", "result = 3")

                        payload, mock_run = _complete_monaco_run(
                            response, {"ok": True, "status_ok_text": "done"}
                        )
                        assert payload == {"type": "saved", "ok": True, "status_ok_text": "done", "markers": []}
                        assert mock_run.call_args.args[2] == "result = 2"
                        assert mock_run.call_args.kwargs["data_range"] == "A1:B2"
                        mock_execute.assert_not_called()


def test_run_python_monaco_reopen_refuses_second_run():
    from plugin.scripting.editor_host import DeferredEditorResult

    ctx = MagicMock()
    doc = MagicMock()
    captured1: dict = {}
    captured2: dict = {}

    def fake_launch1(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured1["on_save"] = on_save
        return True

    def fake_launch2(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured2["on_save"] = on_save
        return True

    try:
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="doc-1"), patch.object(
            pr, "get_runtime_uid", return_value="doc-1"
        ):
            with patch("plugin.scripting.document_scripts.save_user_script"):
                with patch.object(pr, "execute_and_insert_result") as mock_execute:
                    with patch.object(pr, "get_config_str", return_value="Prime Numbers"):
                        with patch(
                            "plugin.scripting.document_scripts.get_user_scripts",
                            return_value={"Prime Numbers": "print(1)"},
                        ):
                            with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch1):
                                ok1 = pr._run_python_monaco(
                                    ctx,
                                    doc,
                                    initial_code="result = 1",
                                    selected_script_name="Prime Numbers",
                                    exe="/venv/bin/python",
                                )
                            assert ok1 is True
                            on_save1 = captured1["on_save"]

                            with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch2):
                                ok2 = pr._run_python_monaco(
                                    ctx,
                                    doc,
                                    initial_code="result = 1",
                                    selected_script_name="Prime Numbers",
                                    exe="/venv/bin/python",
                                )
                            assert ok2 is True
                            on_save2 = captured2["on_save"]

                            # First closure, action "run", returns a DeferredEditorResult and must not call execute_and_insert_result
                            res1 = on_save1("result = 2", False, None, "run")
                            assert isinstance(res1, DeferredEditorResult)
                            mock_execute.assert_not_called()

                            # Second closure, action "run", returns error frame and must not call start_native_script_run again
                            with patch.object(pr, "start_native_script_run") as mock_run:
                                busy = on_save2("result = 3", False, None, "run")
                                assert busy["type"] == "error"
                                assert "already running" in busy["message"]
                                mock_run.assert_not_called()

                            # Second closure, action "save", still returns the saved frame
                            save_response = on_save2("result = 4", False, None, "save")
                            assert save_response == {"type": "saved", "ok": True, "status_ok_text": "Script saved."}

                            # Complete the first deferred result via _complete_monaco_run helper
                            payload, _ = _complete_monaco_run(res1, {"ok": True, "status_ok_text": "done"})
                            assert payload == {"type": "saved", "ok": True, "status_ok_text": "done", "markers": []}

                            # Then the second closure's "run" returns a DeferredEditorResult again
                            res2 = on_save2("result = 5", False, None, "run")
                            assert isinstance(res2, DeferredEditorResult)
    finally:
        pr._monaco_run_in_flight.clear()


def test_run_python_monaco_other_document_can_run():
    from plugin.scripting.editor_host import DeferredEditorResult

    ctx = MagicMock()
    doc_a = MagicMock()
    doc_b = MagicMock()
    captured_a: dict = {}
    captured_b: dict = {}

    def _get_uid(d):
        if d is doc_a:
            return "doc-a"
        if d is doc_b:
            return "doc-b"
        return ""

    def fake_launch_a(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured_a["on_save"] = on_save
        return True

    def fake_launch_b(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured_b["on_save"] = on_save
        return True

    try:
        with patch("plugin.framework.uno_context.get_runtime_uid", side_effect=_get_uid), patch.object(
            pr, "get_runtime_uid", side_effect=_get_uid
        ):
            with patch("plugin.scripting.document_scripts.save_user_script"):
                with patch.object(pr, "get_config_str", return_value="Prime Numbers"):
                    with patch(
                        "plugin.scripting.document_scripts.get_user_scripts",
                        return_value={"Prime Numbers": "print(1)"},
                    ):
                        with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch_a):
                            pr._run_python_monaco(
                                ctx,
                                doc_a,
                                initial_code="result = 1",
                                selected_script_name="Prime Numbers",
                                exe="/venv/bin/python",
                            )
                        with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch_b):
                            pr._run_python_monaco(
                                ctx,
                                doc_b,
                                initial_code="result = 1",
                                selected_script_name="Prime Numbers",
                                exe="/venv/bin/python",
                            )

                        # Start a Run on doc A (do not complete it)
                        res_a = captured_a["on_save"]("result = 2", False, None, "run")
                        assert isinstance(res_a, DeferredEditorResult)

                        # Doc B's on_save(..., "run") returns a DeferredEditorResult, not the already-running error
                        res_b = captured_b["on_save"]("result = 3", False, None, "run")
                        assert isinstance(res_b, DeferredEditorResult)
    finally:
        pr._monaco_run_in_flight.clear()


@pytest.mark.parametrize("empty_name", ["", "   "])
def test_run_python_monaco_empty_name_save_errors_run_still_starts(empty_name):
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["exe"] = exe
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch("plugin.scripting.python_runner.save_selected_script") as mock_save_selected:
            with patch.object(pr, "get_config_str", return_value=empty_name):
                ok = pr._run_python_monaco(
                    ctx,
                    doc,
                    initial_code="result = 1",
                    selected_script_name="",
                    exe="/venv/bin/python",
                )
                assert ok is True

                save_res = captured["on_save"]("result = 2", False, None, "save")
                assert save_res == {
                    "type": "error",
                    "message": "No script is selected. Use New or Save As to keep this code.",
                }
                mock_save_selected.assert_not_called()

                run_res = captured["on_save"]("result = 3", False, None, "run")
                payload, mock_run = _complete_monaco_run(
                    run_res, {"ok": True, "status_ok_text": "done"}
                )
                assert payload == {"type": "saved", "ok": True, "status_ok_text": "done", "markers": []}
                assert mock_run.call_args.args[2] == "result = 3"
                mock_save_selected.assert_not_called()


@pytest.mark.parametrize(
    "return_value, selected_script_name, value, value_2",
    [
        # Document/domain picker names must not be written into My Scripts.
        pytest.param("Regional", "Regional", "new", "new", id="test_run_python_monaco_on_save_does_not_upsert_unknown_user_name"),
        # Document scripts prefixed with [Doc] in config must be stripped and saved to document.
        pytest.param("[Doc] Regional", "[Doc] Regional", "new_code", "new_code", id="test_run_python_monaco_on_save_persists_doc_script_with_prefix"),
    ],
)
def test_run_python_monaco_on_save_does_not_upsert_unknown_user_name(return_value, selected_script_name, value, value_2):
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch("plugin.scripting.document_scripts.save_user_script") as mock_save:
            with patch("plugin.scripting.document_scripts.save_document_script") as mock_doc_save:
                with patch.object(pr, "execute_and_insert_result", return_value={"ok": True}):
                    with patch.object(pr, "get_config_str", return_value=return_value):
                        with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={}):
                            with patch("plugin.scripting.document_scripts._load_document_script_map", return_value=({"Regional": "old"}, None)):
                                pr._run_python_monaco(
                                    ctx,
                                    doc,
                                    initial_code="old",
                                    selected_script_name=selected_script_name,
                                    exe="/venv/bin/python",
                                )
                                captured["on_save"](value, False, None, "save")

    mock_save.assert_not_called()
    mock_doc_save.assert_called_once_with(doc, "Regional", value_2)

def test_run_python_monaco_on_save_errors_when_script_was_deleted():
    """A library name in neither store must not report Save as success."""
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch("plugin.scripting.document_scripts.save_user_script") as mock_save:
            with patch("plugin.scripting.document_scripts.save_document_script") as mock_doc_save:
                with patch.object(pr, "get_config_str", return_value="Gone"):
                    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={}):
                        with patch("plugin.scripting.document_scripts._load_document_script_map", return_value=({}, None)):
                            pr._run_python_monaco(
                                ctx,
                                doc,
                                initial_code="old",
                                selected_script_name="Gone",
                                exe="/venv/bin/python",
                            )
                            response = captured["on_save"]("new", False, None, "save")

    mock_save.assert_not_called()
    mock_doc_save.assert_not_called()
    assert response["type"] == "error"
    assert "Gone" in response["message"]
    assert "not saved" in response["message"]


def test_run_python_monaco_on_save_builtin_still_runs():
    """Built-in templates are not library scripts; Run still executes the buffer."""
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch("plugin.scripting.document_scripts.save_user_script") as mock_save:
            with patch.object(pr, "execute_and_insert_result") as mock_execute:
                with patch.object(pr, "get_config_str", return_value="[Vision] extract_text"):
                    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={}):
                        with patch("plugin.scripting.document_scripts.get_document_scripts", return_value={}):
                            pr._run_python_monaco(
                                ctx,
                                doc,
                                initial_code="# wa_vision extract_text",
                                selected_script_name="[Vision] extract_text",
                                exe="/venv/bin/python",
                            )
                            response = captured["on_save"]("print(1)", False, None, "run")

    mock_save.assert_not_called()
    mock_execute.assert_not_called()
    payload, mock_run = _complete_monaco_run(response, {"ok": True, "status_ok_text": "done"})
    assert payload == {"type": "saved", "ok": True, "status_ok_text": "done", "markers": []}
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["data_range"] is None


def test_execute_and_insert_result_returns_error_on_failure():
    ctx = MagicMock()
    with patch("plugin.scripting.python_runner._prepare_rps_execution", return_value={"early_outcome": None, "t0": 0.0, "py_data": None, "bindings": None, "doc": MagicMock(), "ctx": ctx, "code": "bad()"}):
        with patch.object(pr, "_run_prepared_rps", return_value={"status": "error", "message": "boom"}):
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
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
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
    script_select.getItems.return_value = ["Universal Sample"]

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
                with patch.object(ui, "get_user_scripts", return_value={"Universal Sample": "result = 0"}):
                    with patch.object(ui, "save_user_script") as mock_save:
                        with patch.object(ui, "get_config_str", return_value=""):
                            scheduled = {}

                            def _capture_background(func, *args, **kwargs):
                                scheduled["func"] = func
                                return MagicMock()

                            # start_native_script_run moved to python_runner (#2164) and
                            # spawns from there. Patching only ui left a real worker that
                            # outlived this test and ran under a later test's mocks.
                            with patch.object(ui, "run_in_background", return_value=MagicMock()), \
                                 patch.object(pr, "run_in_background", side_effect=_capture_background):
                                def fake_execute_dialog():
                                    assert code_edit.setFocus.called
                                    listeners[0].actionPerformed(MagicMock())

                                dlg.execute.side_effect = fake_execute_dialog

                                ui.show_python_input_dialog(ctx)

                                dlg.endDialog.assert_not_called()
                                dlg.setVisible.assert_not_called()
                                mock_set.assert_any_call("last_python_script_name_writer", "Universal Sample")
                                mock_save.assert_called_with("Universal Sample", "result = 42")
                                # Run returns before the venv wait. The dialog event
                                # thread must not call execute_and_insert_result.
                                assert scheduled["func"].__name__ == "_native_script_run_worker"
                                btn_run.setEnable.assert_any_call(False)
                                instruction_lbl.setText.assert_any_call("Running...")


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
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
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
    script_select.getItems.return_value = ["Universal Sample"]

    btn_save = MagicMock()
    listeners = []
    btn_save.addActionListener.side_effect = lambda listener: listeners.append(listener)
    
    def fake_get_control(name):
        if name == "BtnSave":
            return btn_save
        if name == "ScriptSelect":
            return script_select
        return code_edit
    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "set_config"):
                with patch.object(ui, "get_user_scripts", return_value={"Universal Sample": "print('hello')"}):
                    with patch.object(ui, "save_user_script") as mock_save:
                        with patch.object(ui, "get_config_str", return_value=""):
                            def fake_execute():
                                listeners[0].actionPerformed(MagicMock())
                            dlg.execute.side_effect = fake_execute

                            ui.show_python_input_dialog(ctx)

                            mock_save.assert_called_with("Universal Sample", "print('hello world')")


def test_persistent_editor_dispatches_script_actions():
    from plugin.scripting.editor_host import PersistentEditor
    from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER

    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.send = MagicMock()
    doc = MagicMock()

    user_scripts = {"MyScript": "print(123)"}

    def _get_config(key):
        if key == "saved_python_scripts":
            return dict(user_scripts)
        return None

    def _set_config(key, value):
        if key == "saved_python_scripts":
            user_scripts.clear()
            user_scripts.update(value)

    with patch("plugin.framework.config.get_config", side_effect=_get_config) as mock_get:
        with patch("plugin.framework.config.get_config_str", return_value="MyScript"):
            with patch("plugin.framework.config.set_config", side_effect=_set_config) as mock_set:
                pe._dispatch_incoming({"type": "request_scripts"})
                mock_get.assert_any_call("saved_python_scripts")
                sent = pe.send.call_args[0][0]
                assert sent["type"] == "scripts_list"
                assert sent["sections"][0]["scripts"] == {"MyScript": "print(123)"}
                assert sent["selected_script_name"] == "MyScript"

                pe._dispatch_incoming({"type": "select_script", "name": "MyScript"})
                mock_set.assert_called_with("last_python_script_name_writer", "MyScript")

                pe._dispatch_incoming({"type": "save_script", "name": "NewScript", "code": "x = 1", "origin": SCRIPT_ORIGIN_USER})
                assert user_scripts == {"MyScript": "print(123)", "NewScript": "x = 1"}

                pe._dispatch_incoming({"type": "delete_script", "name": "MyScript", "origin": SCRIPT_ORIGIN_USER})
                assert user_scripts == {"NewScript": "x = 1"}

    with patch("plugin.scripting.document_scripts.save_document_script", return_value=None) as mock_save_doc:
        pe.set_run_script_document(doc)
        pe._dispatch_incoming({"type": "save_script", "name": "DocScript", "code": "y=2", "origin": SCRIPT_ORIGIN_DOCUMENT})
        mock_save_doc.assert_called_once_with(doc, "DocScript", "y=2")

    pe.send.reset_mock()
    with patch("plugin.scripting.document_scripts.attach_document_script") as mock_attach:
        pe._dispatch_incoming({"type": "attach_script", "name": "Attached", "code": "z=3", "overwrite": True})
        pe.send.assert_not_called()
        mock_attach.assert_not_called()


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
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
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
    script_select.getItems.return_value = ["Universal Sample"]

    btn_save_as = MagicMock()
    listeners = []
    btn_save_as.addActionListener.side_effect = lambda listener: listeners.append(listener)
    
    def fake_get_control(name):
        if name == "ScriptSelect":
            return script_select
        if name == "BtnSaveAs":
            return btn_save_as
        return code_edit
    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "get_user_scripts", return_value={}):
                with patch.object(ui, "get_config_str", return_value=""):
                    with patch.object(ui, "set_config"):
                        with patch.object(ui, "save_user_script") as mock_save:
                            with patch.object(ui, "show_new_script_dialog", return_value=("scriptk", False)) as mock_input:
                                def fake_execute():
                                    listeners[0].actionPerformed(MagicMock())
                                dlg.execute.side_effect = fake_execute

                                ui.show_python_input_dialog(ctx)

                                mock_input.assert_called_once()
                                mock_save.assert_called_with("scriptk", "print('hello world')")


def test_native_dialog_btn_new_action_creates_script():
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
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
        if "Toolkit" in service:
            return toolkit
        return MagicMock()

    smgr.createInstanceWithContext.side_effect = fake_create

    code_edit_model = MagicMock()
    code_edit_model.Text = ""
    code_edit = MagicMock()
    code_edit.getModel.return_value = code_edit_model

    script_select = MagicMock()
    script_select.getSelectedItemPos.return_value = 0
    script_select.getItems.return_value = ["Universal Sample"]

    btn_new = MagicMock()
    listeners = []
    btn_new.addActionListener.side_effect = lambda listener: listeners.append(listener)

    def fake_get_control(name):
        if name == "ScriptSelect":
            return script_select
        if name == "BtnNew":
            return btn_new
        return code_edit

    dlg.getControl.side_effect = fake_get_control

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modal_native():
            with patch.object(ui, "get_user_scripts", return_value={}):
                with patch.object(ui, "get_config_str", return_value=""):
                    with patch.object(ui, "set_config"):
                        with patch.object(ui, "save_user_script") as mock_save:
                            with patch.object(ui, "show_new_script_dialog", return_value=("BrandNewScript", False)) as mock_new_dlg:
                                def fake_execute():
                                    listeners[0].actionPerformed(MagicMock())

                                dlg.execute.side_effect = fake_execute

                                ui.show_python_input_dialog(ctx)

                                mock_new_dlg.assert_called_once()
                                mock_save.assert_called_with(
                                    "BrandNewScript",
                                    '# A simple script\nresult = "Hello from Python!"\n',
                                )


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
        if "DialogProvider" in service:
            dp = MagicMock()
            dp.createDialog.return_value = dlg
            return dp
        if "Toolkit" in service:
            return toolkit
        return MagicMock()

    smgr.createInstanceWithContext.side_effect = fake_create
    code_edit = MagicMock()
    script_select = MagicMock()
    dlg.getControl.side_effect = lambda name: code_edit if name == "CodeEdit" else script_select

    order = []
    dlg.setVisible.side_effect = lambda visible: order.append(("visible", visible))
    code_edit.setFocus.side_effect = lambda: order.append("focus")

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with _patch_modeless_native():
            with patch.object(ui, "get_user_scripts", return_value={}):
                with patch.object(ui, "get_config_str", return_value=""):
                    with patch.object(ui, "set_config"):
                        ui.show_python_input_dialog(ctx)

    dlg.execute.assert_not_called()
    dlg.setVisible.assert_called_once_with(True)
    dlg.addTopWindowListener.assert_called_once()
    assert ("visible", True) in order
    assert "focus" in order
    assert order.index(("visible", True)) < order.index("focus")


def test_picker_select_name_combobox_uses_set_text():
    ctrl = MagicMock(spec=[])
    ctrl.setText = MagicMock()
    ui._picker_select_name(ctrl, "MyScript", ["ScriptA", "MyScript"])
    ctrl.setText.assert_called_once_with("MyScript")


def test_picker_select_name_set_text_follows_select_item_pos():
    ctrl = MagicMock()
    ui._picker_select_name(ctrl, "B", ["A", "B"])
    ctrl.selectItemPos.assert_called_once_with(1, True)
    ctrl.setText.assert_called_once_with("B")



def test_picker_select_name_unknown_name_skips_select_item_pos():
    ctrl = MagicMock()
    ui._picker_select_name(ctrl, "Missing", ["A", "B"])
    ctrl.selectItemPos.assert_not_called()
    ctrl.setText.assert_called_once_with("Missing")


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

def test_save_script_on_builtin_template():
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={}):
        with patch("plugin.scripting.document_scripts.get_document_scripts", return_value={}):
            with patch("plugin.scripting.document_scripts.is_picker_template_name", return_value=True):
                with patch("plugin.scripting.python_runner.launch_monaco_editor") as mock_launch:
                    with patch("plugin.framework.config.get_config_str", return_value="[Vision] extract_text"):
                        pr._run_python_monaco(
                            ctx=ctx,
                            doc=doc,
                            initial_code="print(1)",
                            selected_script_name="[Vision] extract_text",
                            exe="/venv/bin/python",
                        )

                        on_save = mock_launch.call_args.kwargs["on_save"]
                        res = on_save(code="print(1)", _save_as_plain=False, action="save")

    assert res["type"] == "error"
    assert res["message"] == "Built-in helpers are read-only. Use Save As to copy one into My Scripts."


def test_run_python_monaco_on_save_reports_save_user_script_failure():
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"My Script": "old"}):
        with patch("plugin.scripting.document_scripts.get_document_scripts", return_value={}):
            with patch("plugin.scripting.document_scripts.save_user_script", return_value="Permission denied"):
                with patch("plugin.scripting.document_scripts.is_picker_template_name", return_value=False):
                    with patch("plugin.scripting.python_runner.launch_monaco_editor") as mock_launch:
                        with patch.object(pr, "get_config_str", return_value="My Script"):
                            pr._run_python_monaco(
                                ctx=ctx,
                                doc=doc,
                                initial_code="print(1)",
                                selected_script_name="My Script",
                                exe="/venv/bin/python",
                            )

                            on_save = mock_launch.call_args.kwargs["on_save"]
                            res = on_save(code="print(2)", _save_as_plain=False, action="save")

    assert res["type"] == "error"
    assert "Permission denied" in res["message"]


def test_monaco_run_document_key_unrelated_docs_without_uid_differ():
    with patch.object(pr, "get_runtime_uid", return_value=""):
        first, second = object(), object()
        assert pr._monaco_run_document_key(first) != pr._monaco_run_document_key(second)
        assert pr._monaco_run_document_key(first) == pr._monaco_run_document_key(first)


def test_monaco_editor_run_payload_reports_printed_lines():
    payload = pr._monaco_editor_run_payload({"ok": True, "status_ok_text": "done", "stdout": "a\nb\n"}, "ok")
    assert payload["type"] == "saved" and payload["ok"] is True
    assert "2 line(s) printed" in payload["status_ok_text"]


def test_monaco_editor_run_payload_markers_on_failure_and_success():
    code = "x = 1\ny = 2\n"
    msg = "Code parsing failed on line 2 due to: SyntaxError: bad"
    tb = "Traceback (most recent call last):\n  ...\n"
    outcome_fail = {
        "ok": False,
        "message": msg,
        "traceback": tb,
    }
    payload_fail = pr._monaco_editor_run_payload(outcome_fail, "ok", code=code)
    assert payload_fail["type"] == "error"
    assert payload_fail["message"] == msg
    assert payload_fail["traceback"] == tb
    assert payload_fail["markers"] == [
        {
            "line": 2,
            "message": msg,
            "severity": "error",
        }
    ]

    outcome_ok = {
        "ok": True,
        "status_ok_text": "Script executed successfully.",
    }
    payload_ok = pr._monaco_editor_run_payload(outcome_ok, "ok", code=code)
    assert payload_ok["type"] == "saved"
    assert payload_ok["ok"] is True
    assert payload_ok["status_ok_text"] == "Script executed successfully."
    assert payload_ok["markers"] == []
