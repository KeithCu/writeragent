"""LibrePy settings dialog helpers."""

from unittest.mock import MagicMock, patch

from plugin.librepy.settings import (
    _VenvProbeCloseListener,
    _VenvProbeProgressDialog,
    _populate_field,
    _scripting_field_specs,
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

    progress = _VenvProbeProgressDialog(MagicMock())
    progress._dlg = dlg

    with patch("plugin.librepy.settings.set_control_text") as mock_set_text:
        with patch.object(progress, "_pump_events"):
            progress.set_display("probe output")
            progress.set_status("checking numpy")

    mock_set_text.assert_any_call(log_area, "probe output")
    mock_set_text.assert_any_call(status_lbl, "checking numpy")


def test_venv_probe_progress_pumps_events():
    progress = _VenvProbeProgressDialog(MagicMock())
    progress._dlg = MagicMock()
    progress._dlg.getControl.return_value = MagicMock()
    toolkit = MagicMock()

    with patch("plugin.librepy.settings.get_toolkit", return_value=toolkit):
        with patch("plugin.librepy.settings.set_control_text"):
            progress.set_status("warming worker")

    toolkit.processEventsToIdle.assert_called_once()


def test_venv_probe_close_listener_ends_dialog():
    dlg = MagicMock()
    progress = _VenvProbeProgressDialog(MagicMock())
    progress._dlg = dlg
    listener = _VenvProbeCloseListener(progress)

    listener.on_action_performed(None)

    dlg.endDialog.assert_called_once_with(0)
