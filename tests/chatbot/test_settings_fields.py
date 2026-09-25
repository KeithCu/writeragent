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


def test_call_options_provider_skips_plugin_main_when_absent():
    import sys
    import types

    from plugin.chatbot.settings_fields import call_options_provider

    seen: list[object] = []

    def provide(services):
        seen.append(services)
        return ["ok"]

    fake = types.ModuleType("wa_librepy_opts")
    fake.provide = provide
    with patch.dict(sys.modules, {"plugin.main": None, "wa_librepy_opts": fake}):
        out = call_options_provider(MagicMock(), "wa_librepy_opts:provide")
    assert out == ["ok"]
    assert seen == [None]


def test_build_module_field_specs_voice_options_come_from_catalog():
    """Settings voice options follow the catalog, not the short yaml stub."""
    manifest = {
        "name": "audio",
        "config": {
            "tts_voice": {
                "type": "string",
                "widget": "select",
                "options_provider": "plugin.audio.tts_service:settings_voice_options",
                "options": [{"value": "alloy", "label": "alloy (OpenAI Neutral)"}],
            }
        },
    }

    def _cfg(key, default=None):
        values = {
            "audio.tts_voice": "de_DE-thorsten-medium",
            "audio.tts_provider": "piper",
            "audio.tts_model": "",
        }
        return values.get(key, default if default is not None else "")

    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", side_effect=_cfg),
        patch("plugin.audio.tts_service.get_config", side_effect=_cfg),
        patch("plugin.framework.i18n.get_active_locale", return_value="de_DE"),
        patch("plugin.main.get_services", return_value=None),
    ):
        specs = build_module_field_specs("audio", ctx=object(), control_ids="prefixed")

    voice = next(spec for spec in specs if spec["name"] == "audio__tts_voice")
    values = [opt["value"] for opt in voice["options"]]
    assert values[0] == "de_DE-thorsten-medium"
    assert "fr_FR-siwis-medium" in values
    assert "alloy" not in values
    assert "Thorsten" in voice["value"]


def test_build_module_field_specs_options_provider_falls_back_to_yaml():
    manifest = {
        "name": "audio",
        "config": {
            "tts_voice": {
                "type": "string",
                "options_provider": "plugin.audio.tts_service:missing_voice_options",
                "options": [{"value": "default", "label": "default (System Default)"}],
            }
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest]),
        patch("plugin.chatbot.settings_fields.get_config", return_value="default"),
        patch("plugin.main.get_services", return_value=None),
    ):
        specs = build_module_field_specs("audio", ctx=object(), control_ids="flat")

    voice = specs[0]
    assert voice["options"] == [{"value": "default", "label": "default (System Default)"}]
    assert voice["value"] == "default (System Default)"


def test_build_module_field_specs_omits_internal_and_non_persisting():
    manifest_agent = {
        "name": "agent_backend",
        "config": {
            "backend_id": {"type": "string", "widget": "select"},
            "path": {"type": "string", "internal": True},
            "args": {"type": "string", "internal": True},
            "acp_agent_name": {"type": "string", "internal": True},
        },
    }
    manifest_mcp = {
        "name": "mcp",
        "config": {
            "mcp_enabled": {"type": "boolean", "widget": "checkbox"},
            "client_config_snippet": {"type": "string", "widget": "textarea", "settings_persist": False},
            "copy_config": {"type": "string", "widget": "button", "settings_persist": False},
        },
    }
    with (
        patch("plugin._manifest.MODULES", [manifest_agent, manifest_mcp]),
        patch("plugin.chatbot.settings_fields.get_config", return_value=""),
    ):
        agent_specs = build_module_field_specs("agent_backend", control_ids="prefixed")
        mcp_specs = build_module_field_specs("mcp", control_ids="prefixed")

    assert {s["name"] for s in agent_specs} == {"agent_backend__backend_id"}
    assert {s["name"] for s in mcp_specs} == {"mcp__mcp_enabled"}

