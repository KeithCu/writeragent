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
    get_api_key_for_endpoint,
    set_api_key_for_endpoint,
    get_config_bool,
    get_config_int,
    get_config_float,
    get_config_str,
)
from plugin.framework.client.model_fetcher import get_image_model, get_text_model, set_image_model, set_text_model
from plugin.framework.event_bus import global_event_bus

import logging

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
        {"name": "text_model", "value": str(get_text_model())},
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
        from plugin.chatbot.settings_fields import build_module_field_specs

        for m in MODULES:
            m_name = str(m.get("name", ""))
            if m_name in ("main", "ai"):
                continue
            if m.get("settings_tab") is False or m.get("config_dialog"):
                continue

            field_specs.extend(
                build_module_field_specs(m_name, ctx=ctx, control_ids="prefixed")
            )
    except ImportError:
        pass
    return field_specs


def apply_settings_result(ctx, result):
    """Apply settings dialog result to config. Shared by Writer and Calc."""
    from plugin.chatbot.config_ui_helpers import update_lru_history

    field_specs = get_settings_field_specs(ctx)
    field_specs_by_name = {f["name"]: f for f in field_specs}

    # Resolve and validate endpoint first
    if "endpoint" in result:
        set_config("endpoint", result["endpoint"])
        normalized_endpoint = get_current_endpoint()
        if normalized_endpoint:
            update_lru_history(normalized_endpoint, "endpoint_lru", "")
    
    current_endpoint = get_current_endpoint()

    # Apply other keys
    _apply_skip = ("endpoint", "api_key")
    for key, val in result.items():
        if key in _apply_skip or key not in field_specs_by_name:
            continue
            
        save_key = key.replace("__", ".")

        if save_key == "text_model":
            if val:
                set_text_model(val, update_lru=True)
            continue

        set_config(save_key, val)
        _update_lru_for_key(ctx, key, val, current_endpoint)

    if "api_key" in result:
        set_api_key_for_endpoint(current_endpoint, result["api_key"])

    global_event_bus.emit("config:changed", ctx=ctx)


def _update_lru_for_key(ctx, key, val, current_endpoint):
    from plugin.chatbot.config_ui_helpers import update_lru_history
    
    if not val:
        return
        
    if key == "stt_model":
        update_lru_history(val, "audio_model_lru", current_endpoint)
    elif key == "image_model":
        set_image_model(val)
    elif key == "additional_instructions":
        update_lru_history(val, "prompt_lru", "")
    elif key == "image_base_size":
        update_lru_history(str(val), "image_base_size_lru", "")


def _call_options_provider(ctx, provider_path):
    """Import a module and call a function to get options."""
    from plugin.chatbot.settings_fields import call_options_provider

    return call_options_provider(ctx, provider_path)
