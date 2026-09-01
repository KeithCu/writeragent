"""LibrePy settings dialog helpers."""

from unittest.mock import MagicMock, patch

from plugin.librepy.settings import (
    _DownloadVecPackListener,
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

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            self._dlg = MagicMock()

        def run_modal_probe(self, probe_fn, *, title=None):
            if title is not None:
                titles.append(title)
            probe_fn(probe_displays.append, lambda _status: None)
            return True

    listener = _DownloadVecPackListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.librepy.settings.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.native_binaries.run_vec_pack_download", return_value=True) as mock_download,
    ):
        listener.on_action_performed(None)

    mock_download.assert_called_once()
    assert titles and "Cython" in titles[0]


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
        patch("plugin.scripting.native_binaries.ensure_downloaded_audio_on_path") as mock_ensure,
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", return_value=(True, "ok")),
        patch("plugin.scripting.payload_codec.host_cython_status_line", return_value="Cython Accelerator: Inactive (Pure Python)") as mock_status,
    ):
        listener.on_action_performed(None)

    mock_ensure.assert_called_once()
    mock_status.assert_called_once_with(reload=True)


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
        patch("plugin.scripting.native_binaries.ensure_downloaded_audio_on_path"),
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", side_effect=fake_probe),
        patch("plugin.scripting.payload_codec.host_cython_status_line", return_value="ok"),
    ):
        listener.on_action_performed(None)

    assert captured.get("include_vector_search") is False
    assert captured.get("include_audio") is False
