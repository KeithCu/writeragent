"""LibrePy settings dialog helpers."""

from unittest.mock import MagicMock, patch

from plugin.librepy.settings import _populate_field, _scripting_field_specs


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
