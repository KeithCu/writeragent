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
    set_configs,
    get_config,
    get_current_endpoint,
    get_api_key_for_endpoint,
    get_config_bool,
    get_config_int,
    get_config_float,
    get_config_str,
)
from plugin.framework.client.model_fetcher import get_image_model, get_text_model
from plugin.framework.i18n import _

from typing import Any, cast

import logging

log = logging.getLogger(__name__)

# English labels stored in image_default_aspect and mapped to image-tool enums.
# Display strings are aspect_label_gettext(); do not pass the tuple through _().
IMAGE_ASPECT_RATIO_LABELS: tuple[str, ...] = (
    "Square",
    "Landscape (16:9)",
    "Portrait (9:16)",
    "Landscape (3:2)",
    "Portrait (2:3)",
)


def aspect_label_gettext(canonical: str) -> str:
    """Translated combo label for one English aspect string.

    Literals stay inside _() so xgettext extracts them. _(IMAGE_ASPECT_RATIO_LABELS item)
    is invisible to extract, which is why Square stayed English in the UI
    while the catalog already had 正方形 / Cuadrado.
    """
    if canonical == "Square":
        return _("Square")
    if canonical == "Landscape (16:9)":
        return _("Landscape (16:9)")
    if canonical == "Portrait (9:16)":
        return _("Portrait (9:16)")
    if canonical == "Landscape (3:2)":
        return _("Landscape (3:2)")
    if canonical == "Portrait (2:3)":
        return _("Portrait (2:3)")
    return canonical


def canonical_aspect_label(displayed: str) -> str:
    """Map combo text back to the English label stored in config and tool maps."""
    shown = (displayed or "").strip()
    if not shown:
        return "Square"
    for canonical in IMAGE_ASPECT_RATIO_LABELS:
        if shown == canonical or shown == aspect_label_gettext(canonical):
            return canonical
    return shown


def get_settings_field_specs(ctx: Any) -> list[dict[str, Any]]:
    """Return field specs for Settings dialog (single source for dialog and apply keys)."""
    log.debug("get_settings_field_specs entry")
    current_endpoint = get_current_endpoint()
    
    field_specs = []
    field_specs.extend(_get_core_field_specs(ctx, current_endpoint))
    field_specs.extend(_get_image_field_specs(ctx))
    field_specs.extend(_get_module_field_specs(ctx))
    
    return field_specs


def _get_core_field_specs(ctx: Any, current_endpoint: str) -> list[dict[str, Any]]:
    return [
        {"name": "endpoint", "value": get_config_str("endpoint")},
        {"name": "request_timeout", "value": str(get_config_int("request_timeout")), "type": "int"},
        {"name": "text_model", "value": str(get_text_model())},
        {"name": "api_key", "value": str(get_api_key_for_endpoint(current_endpoint))},
        {"name": "temperature", "value": str(get_config_float("temperature")), "type": "float"},
        {"name": "chat_max_tokens", "value": str(get_config_int("chat_max_tokens")), "type": "int"},
        {"name": "additional_instructions", "value": get_config_str("additional_instructions")},
    ]


def _get_image_field_specs(ctx: Any) -> list[dict[str, Any]]:
    stored_aspect = canonical_aspect_label(get_config_str("image_default_aspect") or "Square")
    aspect_options = [
        {"label": aspect_label_gettext(label), "value": label} for label in IMAGE_ASPECT_RATIO_LABELS
    ]
    # value is what the combo shows. Options keep English values so Apply
    # maps the translated label back to the stored string (apply_settings_result).
    display_aspect = stored_aspect
    for opt in aspect_options:
        if opt["value"] == stored_aspect:
            display_aspect = str(opt["label"])
            break
    return [
        {"name": "image_model", "value": str(get_image_model())},
        {"name": "image_base_size", "value": str(get_config_int("image_base_size")), "type": "int"},
        {
            "name": "image_default_aspect",
            "value": display_aspect,
            "options": aspect_options,
        },
        {"name": "image_steps", "value": str(get_config_int("image_steps")), "type": "int"},
        {"name": "image_auto_gallery", "value": "true" if get_config_bool("image_auto_gallery") else "false", "type": "bool"},
        {"name": "image_insert_frame", "value": "true" if get_config_bool("image_insert_frame") else "false", "type": "bool"},
        {"name": "seed", "value": get_config_str("seed")},
    ]


def _get_module_field_specs(ctx: Any) -> list[dict[str, Any]]:
    field_specs = []
    try:
        from plugin._manifest import MODULES
        from plugin.chatbot.settings_fields import build_module_field_specs

        for raw in MODULES:
            m = cast("dict[str, Any]", raw)
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


def apply_settings_result(ctx: Any, result: dict[str, Any]) -> None:
    """Apply settings dialog result to config. Shared by Writer and Calc.

    What was wrong: each field called ``set_config``, which rewrote
    ``writeragent.json`` and emitted ``config:changed``, and this function
    emitted again even when nothing differed. The extra event refreshed the
    sidebar mode combo. ``set_text_model``, ``set_scoped_tts_voice``, and
    ``set_api_key_for_endpoint`` each wrote again before the loop finished.

    Endpoint, text model, API key, voice family, and the other fields go
    through one ``set_configs``. LRU helpers run only after that returns;
    they emit for their own keys. A batch that changes nothing does not emit.
    """
    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text, update_lru_history
    from plugin.chatbot.settings_fields import stored_select_value
    from plugin.framework.client.model_fetcher import _sanitize_stored_model_value
    from plugin.framework.url_utils import normalize_endpoint_url

    field_specs = get_settings_field_specs(ctx)
    field_specs_by_name = {f["name"]: f for f in field_specs}
    pending: dict[str, Any] = {}
    # (dialog key, value) for LRU that does not itself write the field.
    # Recorded before the batch so a value that already matches disk does
    # not refresh the sidebar. Text and image models match set_text_model /
    # set_image_model: no LRU when the sanitized id is already stored.
    ordinary_lru: list[tuple[str, Any]] = []
    text_model_lru: str | None = None
    image_model_lru: str | None = None

    if "endpoint" in result:
        pending["endpoint"] = result["endpoint"]
        # Same normalizer validate() applies, so the API-key slot matches the
        # URL this batch will store. Do not write the endpoint early.
        current_endpoint = endpoint_from_selector_text(str(result["endpoint"] or ""))
    else:
        current_endpoint = get_current_endpoint()

    for key, val in result.items():
        if key in ("endpoint", "api_key") or key not in field_specs_by_name:
            continue

        spec = field_specs_by_name[key]
        save_key = str(spec.get("config_key") or key.replace("__", "."))

        if save_key == "text_model":
            # set_text_model drops placeholders and writes text_model. Doing
            # that here would write before the rest of the batch.
            sanitized = _sanitize_stored_model_value(val)
            if not sanitized:
                continue
            previous = str(get_config("text_model") or "").strip()
            pending["text_model"] = sanitized
            if sanitized != previous:
                text_model_lru = sanitized
            continue

        opts = spec.get("options")
        if isinstance(opts, list):
            val = stored_select_value(val, opts)

        if key in ("audio__stt_provider", "audio.stt_provider"):
            from plugin.audio.stt_service import normalize_stt_provider
            val = normalize_stt_provider(val)
        elif key in ("audio__stt_local_model", "audio.stt_local_model"):
            from plugin.audio.stt_service import normalize_stt_local_model
            val = normalize_stt_local_model(val)
        elif key in ("audio__tts_provider", "audio.tts_provider"):
            from plugin.audio.tts_service import clean_provider_name
            val = clean_provider_name(str(val))
        elif key in ("audio__tts_voice", "audio.tts_voice"):
            from plugin.audio.tts_service import (
                clean_provider_name,
                clean_voice_name,
                get_voice_family,
                voice_choice_to_id,
                voice_options_for_provider,
            )
            # The generic label match above can bind a shared parenthetical
            # (French Female - Siwis) to the provider that was current when
            # the dialog opened. Read the combo text and this result's provider.
            # set_scoped_tts_voice writes both voice keys via set_config; stage
            # them in this batch instead.
            prov = result.get("audio__tts_provider") or result.get("audio.tts_provider") or ""
            model = result.get("audio__tts_model") or result.get("tts_model") or ""
            options = voice_options_for_provider(str(prov), str(model))
            val = voice_choice_to_id(str(result.get(key) or ""), options)
            clean_v = clean_voice_name(val)
            if clean_v:
                prov_clean = clean_provider_name(str(prov))
                voice_endpoint = current_endpoint if "endpoint" in result else None
                family = get_voice_family(prov_clean, str(model), voice_endpoint)
                pending[f"audio.tts_voice_{family}"] = clean_v
                pending["audio.tts_voice"] = clean_v
            else:
                pending["audio.tts_voice"] = val
            continue
        elif key in ("audio__tts_speed", "audio.tts_speed"):
            from plugin.audio.tts_service import parse_tts_speed
            spd = parse_tts_speed(val)
            s_val = str(val).strip()
            val = f"{spd:g}x" if s_val.endswith(("x", "X")) or s_val.startswith("1.0x") else f"{spd:g}"

        if save_key == "image_model":
            sanitized_image = _sanitize_stored_model_value(val)
            previous_image = str(get_config("image_model") or "").strip()
            if sanitized_image and sanitized_image != previous_image:
                image_model_lru = sanitized_image

        pending[save_key] = val
        if val and save_key != "image_model":
            ordinary_lru.append((key, val))

    if "api_key" in result:
        # Same dict set_api_key_for_endpoint would pass to set_config.
        data = get_config("api_keys_by_endpoint")
        if not isinstance(data, dict):
            data = {}
        else:
            data = dict(data)
        data[normalize_endpoint_url(current_endpoint or "")] = str(result["api_key"])
        pending["api_keys_by_endpoint"] = data

    if pending:
        set_configs(pending)

    if "endpoint" in result and current_endpoint:
        update_lru_history(current_endpoint, "endpoint_lru", "")
    if text_model_lru:
        update_lru_history(text_model_lru, "model_lru", current_endpoint)
    if image_model_lru:
        update_lru_history(image_model_lru, "image_model_lru", current_endpoint)
    for key, val in ordinary_lru:
        _update_lru_for_key(ctx, key, val, current_endpoint)


def _update_lru_for_key(ctx: Any, key: str, val: Any, current_endpoint: str) -> None:
    from plugin.chatbot.config_ui_helpers import update_lru_history

    if not val:
        return

    if key in ("audio__stt_model", "stt_model", "audio.stt_model"):
        update_lru_history(val, "audio_model_lru", current_endpoint)
    elif key in ("audio__tts_model", "tts_model", "audio.tts_model"):
        update_lru_history(val, "tts_model_lru", current_endpoint)
    elif key == "image_model":
        # set_image_model writes image_model again. The settings batch already
        # stored it; calling the setter after that saw a match and skipped LRU,
        # or wrote a second time when sanitize differed. Update the list only.
        from plugin.framework.client.model_fetcher import _sanitize_stored_model_value

        sanitized = _sanitize_stored_model_value(val)
        if sanitized:
            update_lru_history(sanitized, "image_model_lru", current_endpoint)
    elif key == "additional_instructions":
        update_lru_history(val, "prompt_lru", "")
    elif key == "image_base_size":
        update_lru_history(str(val), "image_base_size_lru", "")


