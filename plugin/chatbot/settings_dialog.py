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
from plugin.framework.url_utils import normalize_endpoint_url

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


def effective_api_key(
    typed_key: str,
    saved_endpoint: str,
    target_endpoint: str,
    saved_key: str,
    target_key: str,
    user_edited_key: bool | None = None,
) -> str:
    """The key to use for target_endpoint.

    If the user explicitly edited the key for this target_endpoint, use it.
    If the typed key still equals the saved key for the saved_endpoint,
    it belongs to the saved_endpoint, so use the target_endpoint's saved key.
    Otherwise, if the user didn't edit it, it might be a stale custom key from
    the previous endpoint. The safe thing is to use the target's saved key unless
    user_edited_key is True.
    """
    saved_norm = normalize_endpoint_url(saved_endpoint or "")
    target_norm = normalize_endpoint_url(target_endpoint or "")
    typed = str(typed_key)

    if user_edited_key is True:
        return typed

    if target_norm == saved_norm:
        return typed

    if user_edited_key is False:
        if typed == str(target_key or ""):
            return typed
        return str(target_key or "")

    if typed == str(saved_key or ""):
        return str(target_key or "")
    return typed


def endpoint_for_api_key_write(
    typed_key: str,
    saved_endpoint: str,
    target_endpoint: str,
    saved_key: str,
    target_key: str,
    user_edited_key: bool | None = None,
) -> str | None:
    """Normalized endpoint to store *typed_key* under, or None for no write.

    A value that still equals the key stored for the endpoint on disk
    belongs to that endpoint. Return None and leave both slots alone.
    Comparing only against the key already stored for the URL being saved
    writes the previous host's key under the new host: typing a URL leaves
    that key in the field (the box is not rewritten on each keystroke).
    A different value was typed for the URL this OK saves, including a key
    pasted and then a one-character URL correction. An unchanged key for
    the same endpoint is not a write.
    """
    eff_key = effective_api_key(typed_key, saved_endpoint, target_endpoint, saved_key, target_key, user_edited_key)
    if eff_key == str(target_key or ""):
        return None
    return normalize_endpoint_url(target_endpoint or "")


def apply_settings_result(ctx: Any, result: dict[str, Any]) -> None:
    """Apply settings dialog result to config. Shared by Writer and Calc.

    Endpoint, text model, API key, voice family, and the other fields go
    through one ``set_configs``, and only when the value differs from disk
    or from the schema default of a missing key. LRU lists for those keys
    are entries in that same dict. A list that already starts with the new
    value is omitted. A batch that changes nothing does not write and does
    not emit. Per-field ``set_config`` rewrites ``writeragent.json`` and
    emits ``config:changed`` once each, and a second emit refreshes the
    sidebar mode combo.
    """
    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
    from plugin.chatbot.settings_fields import stored_select_value
    from plugin.framework.client.model_fetcher import _sanitize_stored_model_value

    field_specs = get_settings_field_specs(ctx)
    field_specs_by_name = {f["name"]: f for f in field_specs}
    pending: dict[str, Any] = {}
    # (dialog key, value) for LRU that does not itself write the field.
    # Recorded before the batch so a value that already matches disk does
    # not refresh the sidebar. Text and image models match set_text_model /
    # set_image_model: no LRU when the sanitized id is already stored.
    ordinary_lru: list[tuple[str, Any, str]] = []
    text_model_lru: str | None = None
    image_model_lru: str | None = None

    # The key field was filled for this endpoint. A newly typed URL does not
    # rebind the field, so the compare below can tell a stale secret from a
    # key the user typed for the URL being saved.
    saved_endpoint = get_current_endpoint()
    if "endpoint" in result:
        pending["endpoint"] = result["endpoint"]
        # Same normalizer validate() applies, so the API-key slot matches the
        # URL this batch will store. Do not write the endpoint early.
        current_endpoint = endpoint_from_selector_text(str(result["endpoint"] or ""))
    else:
        current_endpoint = saved_endpoint

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
            from plugin.audio.tts_voices import clean_provider_name
            val = clean_provider_name(str(val))
        elif key in ("audio__tts_voice", "audio.tts_voice"):
            from plugin.audio.tts_voices import (
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
            from plugin.audio.tts_voices import parse_tts_speed
            spd = parse_tts_speed(val)
            s_val = str(val).strip()
            val = f"{spd:g}x" if s_val.endswith(("x", "X")) or s_val.startswith("1.0x") else f"{spd:g}"

        if save_key in ("image_model", "audio.stt_model", "audio.tts_model"):
            # Skip empty and placeholder values, and persist the sanitized
            # id — the same value the LRU path already computed. Storing
            # "(Enter API Key to load models)" or "(Connection failed)" as
            # image_model / audio.stt_model / audio.tts_model wipes the
            # configured model when the catalog cannot be listed.
            sanitized_model = _sanitize_stored_model_value(val)
            if not sanitized_model:
                continue
            val = sanitized_model
            if save_key == "image_model":
                previous_image = str(get_config("image_model") or "").strip()
                if val != previous_image:
                    image_model_lru = val

        pending[save_key] = val
        if val and save_key != "image_model":
            ordinary_lru.append((key, val, save_key))

    if "api_key" in result:
        # One slot, not a copy of the whole map. set_configs merges that slot
        # into the map it reads under the config lock. Copying the map here
        # and replacing it in the batch dropped a key another writer stored
        # for a different endpoint between this read and that write.
        typed_key = str(result["api_key"])
        slot = endpoint_for_api_key_write(
            typed_key,
            saved_endpoint,
            current_endpoint,
            str(get_api_key_for_endpoint(saved_endpoint) or ""),
            str(get_api_key_for_endpoint(current_endpoint) or ""),
        )
        if slot is not None:
            pending["api_keys_by_endpoint"] = {slot: typed_key}

    from plugin.chatbot.settings_fields import changed_config_values

    # One set_configs, and only keys that differ from disk or from the
    # schema default of a missing key. Passing the whole dialog made a
    # one-field edit a rewrite of every settings key.
    #
    # The new lists are computed here and stored in the same dict, so
    # one set_configs writes the file once and emits once. A list that
    # already has this value at the head is left out. Calling
    # update_lru_history afterwards writes writeragent.json again for
    # every LRU list.
    changed = changed_config_values(pending, get_config)
    _stage_settings_lru(changed, current_endpoint, text_model_lru, image_model_lru, ordinary_lru)
    if changed:
        set_configs(changed)


def _stage_settings_lru(
    changed: dict[str, Any],
    current_endpoint: str,
    text_model_lru: str | None,
    image_model_lru: str | None,
    ordinary_lru: list[tuple[str, Any, str]],
) -> None:
    """Add LRU list updates for keys already in *changed*. Does not write."""
    if "endpoint" in changed and current_endpoint:
        _stage_lru(changed, current_endpoint, "endpoint_lru", "")
    if text_model_lru and "text_model" in changed:
        _stage_lru(changed, text_model_lru, "model_lru", current_endpoint)
    if image_model_lru and "image_model" in changed:
        _stage_lru(changed, image_model_lru, "image_model_lru", current_endpoint)
    for key, val, save_key in ordinary_lru:
        if save_key not in changed:
            continue
        for item, lru_key, endpoint in _lru_rows_for_key(key, val, current_endpoint):
            _stage_lru(changed, item, lru_key, endpoint)


def _stage_lru(dest: dict[str, Any], val: Any, lru_key: str, endpoint: str) -> None:
    """Put the prepended LRU list into *dest* when it differs from disk.

    Reads through this module's ``get_config`` so a test patch of that name
    is the list this compare sees. A key already staged in *dest* is the
    current list, so two fields that share a list compose in one dict.
    """
    from plugin.chatbot.config_ui_helpers import lru_config_key, next_lru_list
    from plugin.framework.config import LRU_MAX_ITEMS

    scoped = lru_config_key(lru_key, endpoint)
    current = dest[scoped] if scoped in dest else get_config(scoped)
    updated = next_lru_list(current, val, LRU_MAX_ITEMS)
    if updated is not None:
        dest[scoped] = updated


def _lru_rows_for_key(key: str, val: Any, current_endpoint: str) -> list[tuple[Any, str, str]]:
    """``(value, lru list name, endpoint)`` for one settings field.

    Empty when this field has no list. Image models are sanitized the same
    way ``set_image_model`` sanitizes them: the settings batch already stored
    the id, so this only names the list, it does not write ``image_model`` again.
    """
    if not val:
        return []
    if key in ("audio__stt_model", "stt_model", "audio.stt_model"):
        return [(val, "audio_model_lru", current_endpoint)]
    if key in ("audio__tts_model", "tts_model", "audio.tts_model"):
        return [(val, "tts_model_lru", current_endpoint)]
    if key == "image_model":
        from plugin.framework.client.model_fetcher import _sanitize_stored_model_value

        sanitized = _sanitize_stored_model_value(val)
        if not sanitized:
            return []
        return [(sanitized, "image_model_lru", current_endpoint)]
    if key == "additional_instructions":
        return [(val, "prompt_lru", "")]
    if key == "image_base_size":
        return [(str(val), "image_base_size_lru", "")]
    return []


