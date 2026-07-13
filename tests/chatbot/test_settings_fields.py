"""Shared module.yaml settings field-spec helpers."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.settings_fields import apply_field_specs_result, build_module_field_specs


def test_build_module_field_specs_prefixed_and_flat():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string", "widget": "text"},
            "python_exec_timeout": {"type": "int", "widget": "spin"},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch(
            "plugin.chatbot.settings_fields.get_config",
            side_effect=lambda key: {"scripting.python_venv_path": "/venv", "scripting.python_exec_timeout": 30}.get(key, ""),
        ),
    ):
        prefixed = build_module_field_specs("scripting", control_ids="prefixed")
        flat = build_module_field_specs("scripting", control_ids="flat")

    assert {s["name"] for s in prefixed} == {"scripting__python_venv_path", "scripting__python_exec_timeout"}
    assert {s["name"] for s in flat} == {"python_venv_path", "python_exec_timeout"}
    assert all(s.get("config_key", "").startswith("scripting.") for s in prefixed)
    timeout = next(s for s in prefixed if s["name"] == "scripting__python_exec_timeout")
    assert timeout["type"] == "int"
    assert timeout["value"] == "30"


def test_build_module_field_specs_skip_librepy_exclude():
    manifest = {
        "name": "scripting",
        "config": {
            "python_venv_path": {"type": "string"},
            "ppt_master_data_path": {"type": "string", "librepy_exclude": True},
            "test_venv": {"settings_persist": False},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", return_value=""),
    ):
        specs = build_module_field_specs("scripting", control_ids="prefixed", skip_librepy_exclude=True)

    assert {s["name"] for s in specs} == {"scripting__python_venv_path"}


def test_apply_field_specs_result_uses_config_key():
    ctx = MagicMock()
    specs = [
        {"name": "scripting__python_venv_path", "config_key": "scripting.python_venv_path"},
        {"name": "ignored", "config_key": "scripting.ignored"},
    ]
    with (
        patch("plugin.chatbot.settings_fields.set_config") as mock_set,
        patch("plugin.chatbot.settings_fields.global_event_bus") as mock_bus,
    ):
        apply_field_specs_result(ctx, {"scripting__python_venv_path": "/opt/venv"}, specs)

    mock_set.assert_called_once_with("scripting.python_venv_path", "/opt/venv")
    mock_bus.emit.assert_called_once_with("config:changed", ctx=ctx)
