# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Python Test shows a modal dialog with incremental probe output."""

from unittest.mock import MagicMock, call, patch

import pytest

from plugin.framework.i18n import _
from plugin.scripting.venv_probe_ui import (
    ScriptingVenvTestListener,
    VenvProbeProgressDialog,
    _dialog_parent_for_child,
)


def test_python_test_uses_modal_incremental_probe() -> None:
    order: list[str] = []
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()
    probe_displays: list[str] = []
    probe_statuses: list[str] = []
    captured_extra: list[tuple[str, ...]] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            order.append("progress_ctor")
            assert parent_dlg is fake_dlg

        def run_modal_probe(
            self,
            probe_fn,
            *,
            title=None,
            success_title=None,
            failure_title=None,
            append_log=False,
        ):
            order.append("progress_run_modal")
            assert title is None
            assert success_title == "Venv OK"
            assert failure_title == "Venv check failed"
            assert append_log is False
            probe_fn(probe_displays.append, probe_statuses.append)
            return True

    def fake_probe(_raw, on_display, on_status=None, extra_lines_after_header=None, **_kwargs):
        captured_extra.append(tuple(extra_lines_after_header or ()))
        on_display(
            "Python 3.12 responds OK. Cython Accelerator: Inactive (Pure Python)\n\n"
            "Scientific Libraries: numpy\nMissing: pandas"
        )
        if on_status:
            on_status("Scientific Libraries: numpy")
        return True, "Python 3.12 responds OK."

    def fake_status(*, reload=False):
        order.append(f"cython_status_reload={reload}")
        return "Cython Accelerator: Inactive (Pure Python)"

    listener = ScriptingVenvTestListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.scripting.venv_probe_ui.get_optional", return_value=None),
        patch("plugin.scripting.venv_probe_ui.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", side_effect=fake_probe),
        patch("plugin.scripting.payload_codec.host_cython_status_line", side_effect=fake_status),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path"),
    ):
        listener.on_action_performed(MagicMock())

    assert "progress_run_modal" in order
    # Status reload happens on the main thread before the probe worker runs.
    assert order.index("cython_status_reload=True") < order.index("progress_run_modal")
    assert captured_extra == [("Cython Accelerator: Inactive (Pure Python)",)]
    assert any("Scientific Libraries: numpy" in text for text in probe_displays)
    assert any("Cython Accelerator" in text for text in probe_displays)
    assert any("numpy" in status for status in probe_statuses)


def test_set_display_appends_fragments_only_when_enabled() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    log_ctrl = MagicMock()
    mock_dlg = MagicMock()
    mock_dlg.getControl.side_effect = lambda name: log_ctrl if name == "LogArea" else MagicMock()
    progress._dlg = mock_dlg

    with patch("plugin.scripting.venv_probe_ui.set_control_text") as mock_set_text, patch(
        "plugin.scripting.venv_probe_ui.process_events_to_idle"
    ):
        progress._append_log = False
        progress.set_display("one\n")
        assert mock_set_text.call_args[0] == (log_ctrl, "one\n")
        progress.set_display("two\n")
        assert mock_set_text.call_args[0] == (log_ctrl, "two\n")

        progress._append_log = True
        progress._log_parts = []
        progress.set_display("one\n")
        assert mock_set_text.call_args[0] == (log_ctrl, "one\n")
        progress.set_display("two\n")
        assert mock_set_text.call_args[0] == (log_ctrl, "one\ntwo\n")


def test_pump_dialog_skips_nested_process_events() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    progress._dlg = MagicMock()
    call_count = 0

    def fake_process_events(_ctx):
        nonlocal call_count
        call_count += 1
        progress.set_status("nested")

    with (
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle", side_effect=fake_process_events) as mock_process,
        patch("plugin.scripting.venv_probe_ui.set_control_text"),
    ):
        progress.set_status("outer")

    assert call_count == 1
    mock_process.assert_called_once()


def test_finish_leaves_title_when_msgid_missing() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    mock_dlg = MagicMock()
    model = MagicMock()
    model.Title = "Audio Library Download"
    mock_dlg.getModel.return_value = model
    status_ctrl = MagicMock()
    close_ctrl = MagicMock()
    mock_dlg.getControl.side_effect = lambda name: status_ctrl if name == "StatusLbl" else close_ctrl
    progress._dlg = mock_dlg

    with patch("plugin.scripting.venv_probe_ui.set_control_text") as mock_set_text, patch(
        "plugin.scripting.venv_probe_ui.set_control_enabled"
    ), patch("plugin.scripting.venv_probe_ui.process_events_to_idle"):
        progress.finish(None, True)
        assert model.Title == "Audio Library Download"
        assert mock_set_text.call_args[0] == (status_ctrl, "Done")

        progress.finish("", False)
        assert model.Title == "Audio Library Download"
        assert mock_set_text.call_args[0] == (status_ctrl, "Failed")

    translated_calls: list[str] = []

    def fake_translate(msg: str) -> str:
        translated_calls.append(msg)
        return "T:" + msg

    with patch("plugin.scripting.venv_probe_ui._", side_effect=fake_translate), patch(
        "plugin.scripting.venv_probe_ui.set_control_text"
    ), patch("plugin.scripting.venv_probe_ui.set_control_enabled"), patch(
        "plugin.scripting.venv_probe_ui.process_events_to_idle"
    ):
        progress.finish("Venv OK", True)
        assert model.Title == "T:Venv OK"
        assert "Venv OK" in translated_calls
        assert translated_calls.count("Venv OK") == 1


def test_run_modal_probe_keeps_opening_title_and_appends_log() -> None:
    fake_ctx = MagicMock()
    progress = VenvProbeProgressDialog(fake_ctx)
    dlg = MagicMock()
    dlg.getPeer.return_value = None
    model = MagicMock()
    model.Title = ""
    dlg.getModel.return_value = model
    log_area = MagicMock()
    status_lbl = MagicMock()
    btn_close = MagicMock()

    def get_control(name: str):
        if name == "LogArea":
            return log_area
        if name == "StatusLbl":
            return status_lbl
        if name == "BtnClose":
            return btn_close
        return MagicMock()

    dlg.getControl.side_effect = get_control
    toolkit = MagicMock()

    def probe(on_display, on_status):
        on_display("Target directory: /tmp\n")
        on_display("Downloading pack.so\n")
        return True, ""

    log_area_texts: list[str] = []

    def fake_set_control_text(ctrl, text):
        if ctrl is log_area:
            log_area_texts.append(text)

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.get_toolkit", return_value=toolkit),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child", return_value="settings-peer"),
        patch("plugin.scripting.venv_probe_ui.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.scripting.venv_probe_ui.post_to_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled") as mock_set_enabled,
        patch("plugin.scripting.venv_probe_ui.set_control_text", side_effect=fake_set_control_text),
    ):
        progress.run_modal_probe(probe, title="Audio Library Download", append_log=True)

    assert model.Title == _("Audio Library Download")
    assert model.Title != "Venv OK"
    assert log_area_texts[-1] == "Target directory: /tmp\nDownloading pack.so\n"
    dlg.createPeer.assert_called_once_with(toolkit, "settings-peer")
    dlg.setVisible.assert_called_once_with(False)
    visible_idx = dlg.mock_calls.index(call.setVisible(False))
    peer_idx = dlg.mock_calls.index(call.createPeer(toolkit, "settings-peer"))
    assert visible_idx < peer_idx
    enabled_calls = [c for c in mock_set_enabled.call_args_list if c[0][0] is btn_close]
    assert enabled_calls[0] == call(btn_close, False)
    assert enabled_calls[-1] == call(btn_close, True)
    dlg.execute.assert_called_once()


def test_run_modal_probe_applies_result_titles_once() -> None:
    def make_harness(ok_result: bool):
        fake_ctx = MagicMock()
        progress = VenvProbeProgressDialog(fake_ctx)
        dlg = MagicMock()
        dlg.getPeer.return_value = None
        model = MagicMock()
        model.Title = ""
        dlg.getModel.return_value = model
        dlg.getControl.return_value = MagicMock()

        def probe(on_display, on_status):
            return ok_result, "msg"

        return progress, dlg, model, probe

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail") as mock_load,
        patch("plugin.scripting.venv_probe_ui.get_toolkit"),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child"),
        patch("plugin.scripting.venv_probe_ui.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.scripting.venv_probe_ui.post_to_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.set_control_text"),
        patch("plugin.scripting.venv_probe_ui._", side_effect=lambda msg: "T:" + msg),
    ):
        p1, dlg1, model1, probe1 = make_harness(False)
        mock_load.return_value = (dlg1, None)
        p1.run_modal_probe(probe1, success_title="Venv OK", failure_title="Venv check failed")
        assert model1.Title == "T:Venv check failed"

        p2, dlg2, model2, probe2 = make_harness(True)
        mock_load.return_value = (dlg2, None)
        p2.run_modal_probe(probe2, success_title="Venv OK", failure_title="Venv check failed")
        assert model2.Title == "T:Venv OK"


def test_run_modal_probe_appends_exception_text() -> None:
    fake_ctx = MagicMock()
    progress = VenvProbeProgressDialog(fake_ctx)
    dlg = MagicMock()
    dlg.getPeer.return_value = None
    model = MagicMock()
    model.Title = ""
    dlg.getModel.return_value = model
    log_area = MagicMock()
    dlg.getControl.side_effect = lambda name: log_area if name == "LogArea" else MagicMock()

    def probe(on_display, on_status):
        on_display("Target directory: /tmp\n")
        raise RuntimeError("boom")

    log_area_texts: list[str] = []

    def fake_set_control_text(ctrl, text):
        if ctrl is log_area:
            log_area_texts.append(text)

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.get_toolkit"),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child"),
        patch("plugin.scripting.venv_probe_ui.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.scripting.venv_probe_ui.post_to_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.set_control_text", side_effect=fake_set_control_text),
        patch("plugin.scripting.venv_probe_ui._", side_effect=lambda msg: "T:" + msg),
    ):
        progress.run_modal_probe(
            probe,
            append_log=True,
            failure_title="Venv check failed",
        )

    assert log_area_texts[-1] == "Target directory: /tmp\nboom"
    assert model.Title == "T:Venv check failed"


def test_run_modal_probe_skips_worker_when_dialog_missing() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    with (
        patch.object(progress, "_create_dialog", return_value=False),
        patch("plugin.scripting.venv_probe_ui.run_in_background") as mock_worker,
    ):
        progress.run_modal_probe(MagicMock())

    mock_worker.assert_not_called()


def test_create_dialog_skips_create_peer_when_peer_exists() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    dlg = MagicMock()
    dlg.getPeer.return_value = "existing-peer"
    btn_close = MagicMock()
    dlg.getControl.side_effect = lambda name: btn_close if name == "BtnClose" else MagicMock()

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled") as mock_set_enabled,
    ):
        progress._create_dialog()

    dlg.createPeer.assert_not_called()
    mock_set_enabled.assert_called_with(btn_close, False)


def test_create_peer_failure_still_returns_dialog() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    dlg = MagicMock()
    dlg.getPeer.return_value = None
    dlg.createPeer.side_effect = RuntimeError("peer error")

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.get_toolkit", return_value=MagicMock()),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child", return_value=None),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
    ):
        progress._create_dialog()

    assert progress._dlg is dlg


def test_dialog_parent_for_child_prefers_settings_peer() -> None:
    parent = MagicMock()
    parent.getPeer.return_value = "settings-peer"
    assert _dialog_parent_for_child(MagicMock(), parent) == "settings-peer"
    parent.getPeer.assert_called_once()


def test_dialog_parent_for_child_falls_back_to_frame() -> None:
    fake_desktop = MagicMock()
    fake_frame = MagicMock()
    fake_frame.getContainerWindow.return_value = "frame-win"
    fake_desktop.getCurrentFrame.return_value = fake_frame

    with patch("plugin.scripting.venv_probe_ui.get_desktop", return_value=fake_desktop):
        assert _dialog_parent_for_child(MagicMock(), None) == "frame-win"

    parent_with_broken_peer = MagicMock()
    parent_with_broken_peer.getPeer.side_effect = RuntimeError("no peer")
    with patch("plugin.scripting.venv_probe_ui.get_desktop", return_value=fake_desktop):
        assert _dialog_parent_for_child(MagicMock(), parent_with_broken_peer) == "frame-win"

    with patch("plugin.scripting.venv_probe_ui.get_desktop", side_effect=RuntimeError("no desktop")):
        assert _dialog_parent_for_child(MagicMock(), None) is None


def test_create_dialog_missing_dialog_calls_msgbox_and_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    fake_ctx = MagicMock()
    progress = VenvProbeProgressDialog(fake_ctx)
    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(None, "detail text")),
        patch("plugin.scripting.venv_probe_ui.msgbox") as mock_msgbox,
    ):
        with caplog.at_level("WARNING"):
            result = progress._create_dialog()

    assert result is False
    assert "detail text" in caplog.text
    mock_msgbox.assert_called_once()


def test_partial_creation_disposes() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    dlg = MagicMock()

    def fake_create(**kwargs):
        progress._dlg = dlg
        raise RuntimeError("setup failed")

    with patch.object(progress, "_create_dialog", side_effect=fake_create):
        with pytest.raises(RuntimeError, match="setup failed"):
            progress.run_modal_probe(MagicMock())

    dlg.dispose.assert_called_once()


def test_run_modal_probe_failure_without_display_writes_msg_to_log_area() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    dlg = MagicMock()
    log_area = MagicMock()
    dlg.getControl.side_effect = lambda name: log_area if name == "LogArea" else MagicMock()

    def probe(on_display, on_status):
        return False, "probe failed"

    log_area_texts: list[str] = []

    def fake_set_control_text(ctrl, text):
        if ctrl is log_area:
            log_area_texts.append(text)

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.get_toolkit"),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child"),
        patch("plugin.scripting.venv_probe_ui.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.scripting.venv_probe_ui.post_to_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.get_control_text", return_value=""),
        patch("plugin.scripting.venv_probe_ui.set_control_text", side_effect=fake_set_control_text),
    ):
        progress.run_modal_probe(probe)

    assert log_area_texts[-1] == "probe failed"


def test_run_modal_probe_keyboard_interrupt_posted_and_reraised() -> None:
    progress = VenvProbeProgressDialog(MagicMock())
    dlg = MagicMock()
    status_lbl = MagicMock()
    dlg.getControl.side_effect = lambda name: status_lbl if name == "StatusLbl" else MagicMock()

    def probe(on_display, on_status):
        raise KeyboardInterrupt("stop")

    status_texts: list[str] = []

    def fake_set_control_text(ctrl, text):
        if ctrl is status_lbl:
            status_texts.append(text)

    with (
        patch("plugin.scripting.venv_probe_ui.load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch("plugin.scripting.venv_probe_ui.get_toolkit"),
        patch("plugin.scripting.venv_probe_ui._dialog_parent_for_child"),
        patch("plugin.scripting.venv_probe_ui.run_in_background", side_effect=lambda fn, **kw: fn()),
        patch("plugin.scripting.venv_probe_ui.post_to_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.set_control_text", side_effect=fake_set_control_text),
    ):
        with pytest.raises(KeyboardInterrupt):
            progress.run_modal_probe(probe)

    assert _("Failed") in status_texts
