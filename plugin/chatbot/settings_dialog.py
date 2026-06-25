# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from plugin.framework.config import (
    get_config,
    set_config,
    get_current_endpoint,
    as_bool,
    get_api_key_for_endpoint,
    set_api_key_for_endpoint,
    get_config_bool,
    get_config_int,
    get_config_float,
    get_config_str,
    parse_int_robust,
)
from plugin.framework.client.model_fetcher import get_image_model, set_image_model
from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
from plugin.framework.event_bus import global_event_bus

import logging
from plugin.framework.i18n import _

log = logging.getLogger(__name__)


def get_settings_field_specs(ctx):
    """Return field specs for Settings dialog (single source for dialog and apply keys)."""
    log.debug("get_settings_field_specs entry")
    current_endpoint = get_current_endpoint()
    
    field_specs = []
    field_specs.extend(_get_core_field_specs(ctx, current_endpoint))
    field_specs.extend(_get_image_field_specs(ctx))
    field_specs.extend(_get_module_field_specs(ctx))
    
    return field_specs


def _get_core_field_specs(ctx, current_endpoint):
    return [
        {"name": "endpoint", "value": get_config_str("endpoint")},
        {"name": "request_timeout", "value": str(get_config_int("request_timeout")), "type": "int"},
        {"name": "text_model", "value": str(get_config("text_model") or get_config("model") or "")},
        {"name": "api_key", "value": str(get_api_key_for_endpoint(current_endpoint))},
        {"name": "temperature", "value": str(get_config_float("temperature")), "type": "float"},
        {"name": "chat_max_tokens", "value": str(get_config_int("chat_max_tokens")), "type": "int"},
        {"name": "additional_instructions", "value": get_config_str("additional_instructions")},
        {"name": "stt_model", "value": str(get_config("stt_model") or "")},
        # Text analytics sentiment (JSON overridable; for now only transformers engine with multilingual model).
        {"name": "text_analytics_sentiment_model", "value": str(get_config("text_analytics_sentiment_model") or "")},
        {"name": "text_analytics_sentiment_engine", "value": str(get_config("text_analytics_sentiment_engine") or "")},
    ]


def _get_image_field_specs(ctx):
    return [
        {"name": "image_model", "value": str(get_image_model())},
        {"name": "image_base_size", "value": str(get_config_int("image_base_size")), "type": "int"},
        {"name": "image_default_aspect", "value": get_config_str("image_default_aspect")},
        {"name": "image_steps", "value": str(get_config_int("image_steps")), "type": "int"},
        {"name": "image_auto_gallery", "value": "true" if get_config_bool("image_auto_gallery") else "false", "type": "bool"},
        {"name": "image_insert_frame", "value": "true" if get_config_bool("image_insert_frame") else "false", "type": "bool"},
        {"name": "seed", "value": get_config_str("seed")},
    ]


def _get_module_field_specs(ctx):
    field_specs = []
    try:
        from plugin._manifest import MODULES

        for m in MODULES:
            m_name = str(m.get("name", ""))
            if m_name in ("main", "ai"):
                continue
            if m.get("settings_tab") is False or m.get("config_dialog"):
                continue

            m_config = m.get("config", {})
            if not isinstance(m_config, dict):
                m_config = {}

            for field_name, schema in m_config.items():
                if not isinstance(schema, dict):
                    continue

                if schema.get("internal") or schema.get("widget") == "list_detail":
                    continue
                # Action-only controls (e.g. Test) exist in XDL but are not load/save fields.
                if schema.get("settings_persist") is False:
                    continue

                prefix = m_name.replace(".", "_")
                ctrl_id = f"{prefix}__{field_name}"
                config_key = f"{m_name}.{field_name}"

                val = get_config(config_key)
                opts = schema.get("options", [])

                # For select/combo with value/label options, use label for display so dropdown shows correctly
                if isinstance(opts, list) and opts and isinstance(opts[0], dict):
                    v_str = str(val).strip().lower()
                    for o in opts:
                        if isinstance(o, dict) and str(o.get("value", "")) == v_str:
                            val = _(str(o.get("label", val)))
                            break

                field: dict = {"name": ctrl_id, "value": str(val)}

                # Resolve dynamic options if options_provider is present; else use schema options
                provider_path = schema.get("options_provider")
                if provider_path and isinstance(provider_path, str):
                    try:
                        field["options"] = _call_options_provider(ctx, provider_path)
                    except Exception as e:
                        log.error(f"Failed to resolve options_provider {provider_path}: {e}")
                elif schema.get("options"):
                    field["options"] = schema["options"]

                schema_type = schema.get("type", "string")
                if schema_type == "boolean":
                    schema_type = "bool"
                if schema_type in ("bool", "int", "float"):
                    field["type"] = str(schema_type)
                    if schema_type == "bool":
                        field["value"] = "true" if as_bool(val) else "false"

                field_specs.append(field)
    except ImportError:
        pass
    return field_specs


def apply_settings_result(ctx, result):
    """Apply settings dialog result to config. Shared by Writer and Calc."""
    from plugin.chatbot.config_ui_helpers import update_lru_history

    field_specs = get_settings_field_specs(ctx)
    field_specs_by_name = {f["name"]: f for f in field_specs}
    int_field_names = {f["name"] for f in field_specs if f.get("type") == "int"}

    # Resolve endpoint first
    effective_endpoint = endpoint_from_selector_text(result.get("endpoint", "")) if "endpoint" in result else get_current_endpoint()
    if "endpoint" in result and effective_endpoint:
        set_config("endpoint", effective_endpoint)
        update_lru_history(effective_endpoint, "endpoint_lru", "")
    
    current_endpoint = effective_endpoint or get_current_endpoint()

    # Apply most keys directly
    _apply_skip = ("endpoint", "api_key")
    for key, val in result.items():
        if key in _apply_skip or key not in field_specs_by_name:
            continue
            
        spec = field_specs_by_name[key]
        save_key = key.replace("__", ".")

        # Type conversion
        if key in int_field_names:
            try:
                val = parse_int_robust(val)
            except ValueError:
                pass
        
        # Map translated label back to value
        if "options" in spec and val:
            val_str = str(val)
            for opt in spec["options"]:
                if isinstance(opt, dict):
                    # We compare translated labels to the UI result to map back to original value
                    lbl = str(opt.get("label", opt.get("value", "")))
                    if _(lbl) == val_str:
                        val = opt.get("value", lbl)
                        break

        # Special validation for temperature
        if save_key == "temperature":
            try:
                f_val = float(val)
                if f_val > 1.0:
                    from .dialog_views import msgbox
                    msgbox(ctx, _("Invalid Setting"), _("Temperature must be <= 1.0"))
                    continue
                if f_val < 0:
                    val = -1.0
            except (ValueError, TypeError):
                pass

        set_config(save_key, val)
        _update_lru_for_key(ctx, key, val, current_endpoint)

    if "api_key" in result:
        set_api_key_for_endpoint(current_endpoint, result["api_key"])

    global_event_bus.emit("config:changed", ctx=ctx)


def _update_lru_for_key(ctx, key, val, current_endpoint):
    from plugin.chatbot.config_ui_helpers import update_lru_history
    
    if not val:
        return
        
    if key == "text_model":
        update_lru_history(val, "model_lru", current_endpoint)
    elif key == "stt_model":
        update_lru_history(val, "audio_model_lru", current_endpoint)
    elif key == "image_model":
        set_image_model(val)
    elif key == "additional_instructions":
        update_lru_history(val, "prompt_lru", "")
    elif key == "image_base_size":
        update_lru_history(str(val), "image_base_size_lru", "")


def _call_options_provider(ctx, provider_path):
    """Import a module and call a function to get options."""
    log.debug(f"_call_options_provider: {provider_path}")
    try:
        module_path, func_name = provider_path.rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)

        from plugin.main import get_services
        services = get_services()
        options = func(services)
        log.debug(f"_call_options_provider success: {len(options)} options returned")
        return options
    except Exception as e:
        log.error(f"_call_options_provider FAILED for {provider_path}: {e}")
        import traceback
        log.error(traceback.format_exc())
        from plugin.framework.errors import ConfigError
        raise ConfigError(f"Options provider {provider_path} failed: {e}") from e
