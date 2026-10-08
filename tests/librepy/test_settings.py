"""LibrePy settings dialog helpers."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.librepy.settings import (
    _DownloadVecPackListener,
    _extract_field,
    _populate_field,
    _scripting_field_specs,
)
from plugin.scripting.venv_probe_ui import (
    ScriptingVenvTestListener,
    VenvProbeProgressDialog,
    _VenvProbeCloseListener,
)


def test_scripting_field_specs_skips_buttons_and_internal():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string", "widget": "text", "label": "Path"},
            "test_venv": {"type": "string", "widget": "button", "settings_persist": False},
            "force_internal_script_editor": {"type": "bool", "internal": True},
        },
    }
    with patch("plugin._manifest.MODULES", [manifest]):
        specs = _scripting_field_specs()
    names = {s["name"] for s in specs}
    assert names == {"scripting__python_venv_path"}


def test_scripting_field_specs_skips_librepy_exclude():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string", "widget": "text", "label": "Path"},
            "ppt_master_data_path": {"type": "string", "widget": "folder", "librepy_exclude": True},
            "test_ppt_master_data": {"type": "string", "widget": "button", "librepy_exclude": True},
        },
    }
    with patch("plugin._manifest.MODULES", [manifest]):
        specs = _scripting_field_specs()
    names = {s["name"] for s in specs}
    assert names == {"scripting__python_venv_path"}


def test_populate_field_uses_setvalue_for_numeric():
    class _NumericCtrl:
        def setValue(self, value):
            self.value = value

    ctrl = _NumericCtrl()
    _populate_field(ctrl, {"name": "scripting__python_exec_timeout", "type": "int", "value": "42"})
    assert ctrl.value == 42.0


def test_extract_field_checkbox_string_and_numeric_getvalue():
    class _Box:
        def supportsService(self, name):
            return False

        def getState(self):
            return 1

    class _Off:
        def supportsService(self, name):
            return False

        def getState(self):
            return 0

    class _Spin:
        def getText(self):
            return "0"

        def getValue(self):
            return 7

    assert _extract_field(_Box(), {"type": "bool"}) == "true"
    assert _extract_field(_Off(), {"type": "bool"}) == "false"
    assert _extract_field(_Spin(), {"type": "int"}) == "7"


def test_venv_probe_progress_uses_xdl_control_ids():
    log_area = MagicMock()
    status_lbl = MagicMock()
    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: {
        "LogArea": log_area,
        "StatusLbl": status_lbl,
    }[name]

    progress = VenvProbeProgressDialog(MagicMock())
    progress._dlg = dlg

    with patch("plugin.scripting.venv_probe_ui.set_control_text") as mock_set_text:
        with patch("plugin.scripting.venv_probe_ui.process_events_to_idle"):
            progress.set_display("probe output")
            progress.set_status("checking numpy")

    mock_set_text.assert_any_call(log_area, "probe output")
    mock_set_text.assert_any_call(status_lbl, "checking numpy")


def test_venv_probe_progress_pumps_events():
    ctx = MagicMock()
    progress = VenvProbeProgressDialog(ctx)
    progress._dlg = MagicMock()
    progress._dlg.getControl.return_value = MagicMock()

    with (
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle") as mock_pump,
        patch("plugin.scripting.venv_probe_ui.set_control_text"),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
    ):
        progress.set_status("warming worker")

    mock_pump.assert_called_once_with(ctx)


def test_venv_probe_progress_finish_pumps_only_on_main_thread() -> None:
    ctx = MagicMock()
    progress = VenvProbeProgressDialog(ctx)
    progress._dlg = MagicMock()
    progress._dlg.getControl.return_value = MagicMock()

    with (
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle") as mock_pump,
        patch("plugin.scripting.venv_probe_ui.set_control_text"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=False),
    ):
        progress.finish("Venv check failed", False)

    mock_pump.assert_not_called()

    with (
        patch("plugin.scripting.venv_probe_ui.process_events_to_idle") as mock_pump,
        patch("plugin.scripting.venv_probe_ui.set_control_text"),
        patch("plugin.scripting.venv_probe_ui.set_control_enabled"),
        patch("plugin.scripting.venv_probe_ui.on_main_thread", return_value=True),
    ):
        progress.finish("Venv OK", True)

    mock_pump.assert_called_once_with(ctx)


def test_venv_probe_close_listener_ends_dialog():
    dlg = MagicMock()
    progress = VenvProbeProgressDialog(MagicMock())
    progress._dlg = dlg
    listener = _VenvProbeCloseListener(progress)

    listener.on_action_performed(None)

    dlg.endDialog.assert_called_once_with(0)


def test_download_vec_pack_listener_runs_vec_only_download() -> None:
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()
    probe_displays: list[str] = []
    titles: list[str] = []
    order: list[str] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            self._dlg = MagicMock()

        def run_modal_probe(self, probe_fn, *, title=None):
            if title is not None:
                titles.append(title)
            # The real dialog runs probe_fn on a worker. This stand-in calls it
            # here so a direct sys.path mutation in the probe is visible.
            probe_fn(probe_displays.append, lambda _status: None)
            return True

    def fake_download(_on_display, _on_status, **kwargs):
        order.append("download")
        assert kwargs.get("bind_host") is False
        return True

    def fake_execute(fn, *args, **kwargs):
        order.append("hop")
        fn(*args, **kwargs)

    def fake_ensure():
        order.append("ensure")

    def fake_invalidate():
        order.append("invalidate")

    listener = _DownloadVecPackListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.librepy.settings.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.native_binaries.run_vec_pack_download", side_effect=fake_download),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=fake_execute),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path", side_effect=fake_ensure),
        patch("plugin.scripting.payload_codec.invalidate_host_cython_accelerator", side_effect=fake_invalidate),
    ):
        listener.on_action_performed(None)

    assert titles and "Cython" in titles[0]
    assert order == ["download", "hop", "ensure", "invalidate"]


def test_download_vec_pack_listener_skips_bind_when_download_fails() -> None:
    order: list[str] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            pass

        def run_modal_probe(self, probe_fn, *, title=None):
            probe_fn(lambda _text: None, lambda _status: None)

    def fake_download(_on_display, _on_status, **kwargs):
        order.append("download")
        assert kwargs.get("bind_host") is False
        return False

    listener = _DownloadVecPackListener(MagicMock(), MagicMock())
    with (
        patch("plugin.librepy.settings.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.native_binaries.run_vec_pack_download", side_effect=fake_download),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=AssertionError("hop")),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path", side_effect=AssertionError("ensure")),
        patch("plugin.scripting.payload_codec.invalidate_host_cython_accelerator", side_effect=AssertionError("invalidate")),
    ):
        listener.on_action_performed(None)

    assert order == ["download"]


def test_venv_test_listener_ensures_downloaded_vec_on_path() -> None:
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            pass

        def run_modal_probe(self, probe_fn, *, title=None):
            probe_fn(lambda _text: None, lambda _status: None)
            return True

    listener = ScriptingVenvTestListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.scripting.venv_probe_ui.get_optional", return_value=None),
        patch("plugin.scripting.venv_probe_ui.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path") as mock_ensure,
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", return_value=(True, "ok")),
        patch("plugin.scripting.payload_codec.host_cython_status_line", return_value="Cython Accelerator: Inactive (Pure Python)") as mock_status,
    ):
        listener.on_action_performed(None)

    mock_ensure.assert_called_once()
    mock_status.assert_called_once_with(reload=True)


def test_open_librepy_settings_disposes_when_chrome_raises() -> None:
    from plugin.librepy.settings import open_librepy_settings

    dlg = MagicMock()
    with (
        patch("plugin.librepy.settings.init_logging"),
        patch("plugin.librepy.settings.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.librepy.settings._configure_librepy_settings_chrome", side_effect=RuntimeError("chrome")),
    ):
        with pytest.raises(RuntimeError, match="chrome"):
            open_librepy_settings(MagicMock())

    dlg.execute.assert_not_called()
    dlg.dispose.assert_called_once()


def test_open_librepy_settings_disposes_when_populate_raises() -> None:
    from plugin.librepy.settings import open_librepy_settings

    dlg = MagicMock()
    ctrl = MagicMock()

    def get_opt(_dlg, name):
        if name == "scripting__python_venv_path":
            return ctrl
        return None

    with (
        patch("plugin.librepy.settings.init_logging"),
        patch("plugin.librepy.settings.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.librepy.settings._configure_librepy_settings_chrome"),
        patch(
            "plugin.librepy.settings._scripting_field_specs",
            return_value=[{"name": "scripting__python_venv_path", "type": "string", "value": "x"}],
        ),
        patch("plugin.librepy.settings.get_optional", side_effect=get_opt),
        patch("plugin.librepy.settings._populate_field", side_effect=RuntimeError("populate")),
        patch("plugin.librepy.settings.translate_dialog"),
    ):
        with pytest.raises(RuntimeError, match="populate"):
            open_librepy_settings(MagicMock())

    dlg.execute.assert_not_called()
    dlg.dispose.assert_called_once()


def test_open_librepy_settings_dispose_error_keeps_populate_error() -> None:
    from plugin.librepy.settings import open_librepy_settings

    dlg = MagicMock()
    dlg.dispose.side_effect = RuntimeError("dispose failed")

    with (
        patch("plugin.librepy.settings.init_logging"),
        patch("plugin.librepy.settings.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.librepy.settings._configure_librepy_settings_chrome", side_effect=RuntimeError("chrome")),
    ):
        with pytest.raises(RuntimeError, match="^chrome$"):
            open_librepy_settings(MagicMock())

    dlg.dispose.assert_called_once()


def test_open_librepy_settings_disposes_after_cancel() -> None:
    from plugin.librepy.settings import open_librepy_settings

    dlg = MagicMock()
    dlg.execute.return_value = 0
    with (
        patch("plugin.librepy.settings.init_logging"),
        patch("plugin.librepy.settings.load_writeragent_dialog_detail", return_value=(dlg, "")),
        patch("plugin.librepy.settings._configure_librepy_settings_chrome"),
        patch("plugin.librepy.settings._scripting_field_specs", return_value=[]),
        patch("plugin.librepy.settings.get_optional", return_value=None),
        patch("plugin.librepy.settings.translate_dialog"),
    ):
        open_librepy_settings(MagicMock())

    dlg.execute.assert_called_once()
    dlg.dispose.assert_called_once()


def test_open_librepy_settings_msgbox_on_null_dialog() -> None:
    from plugin.librepy.settings import open_librepy_settings

    with (
        patch("plugin.librepy.settings.init_logging"),
        patch(
            "plugin.librepy.settings.load_writeragent_dialog_detail",
            return_value=(None, "missing xdl"),
        ),
        patch("plugin.librepy.settings.msgbox") as mock_msgbox,
    ):
        open_librepy_settings(MagicMock())

    mock_msgbox.assert_called_once()
    args, kwargs = mock_msgbox.call_args
    assert "Could not open Settings" in str(args[2])
    assert kwargs.get("box_type") == 3


def test_configure_librepy_settings_overrides_download_label() -> None:
    from plugin.librepy.settings import _configure_librepy_settings_chrome

    label = MagicMock()
    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: {
        "label_scripting__download_audio_binaries": label,
    }.get(name)

    with (
        patch("plugin.librepy.settings.get_optional", side_effect=lambda _dlg, name: {
            "label_scripting__download_audio_binaries": label,
        }.get(name)),
        patch("plugin.librepy.settings.set_control_text") as mock_set,
        patch("plugin.librepy.settings.set_control_visible"),
    ):
        _configure_librepy_settings_chrome(dlg)

    mock_set.assert_called()
    assert any("Cython" in str(call.args[1]) for call in mock_set.call_args_list)


def test_librepy_venv_test_listener_skips_embeddings_and_audio() -> None:
    captured: dict[str, object] = {}

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            pass

        def run_modal_probe(self, probe_fn, *, title=None):
            probe_fn(lambda _text: None, lambda _status: None)
            return True

    def fake_probe(*_args, **kwargs):
        captured.update(kwargs)
        return True, "ok"

    listener = ScriptingVenvTestListener(
        MagicMock(), MagicMock(), include_vector_search=False, include_audio=False
    )
    with (
        patch("plugin.scripting.venv_probe_ui.get_optional", return_value=None),
        patch("plugin.scripting.venv_probe_ui.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path"),
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", side_effect=fake_probe),
        patch("plugin.scripting.payload_codec.host_cython_status_line", return_value="ok"),
    ):
        listener.on_action_performed(None)

    assert captured.get("include_vector_search") is False
    assert captured.get("include_audio") is False
