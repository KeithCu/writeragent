# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""TTS voice catalog, scoped voice resolution, and voice settings helpers."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from plugin.audio.voice_catalog import (
    DEFAULT_VOICE_FOR_FAMILY,
    KOKORO_CATALOG_ITEMS as _KOKORO_CATALOG_ITEMS,
    KOKORO_FALLBACK_VOICE as _KOKORO_FALLBACK_VOICE,
    KOKORO_OPENAI_ALIASES as _KOKORO_VOICES,
    LOCALE_TO_KOKORO_DEFAULT as _LOCALE_TO_KOKORO_DEFAULT,
    PIPER_FALLBACK_VOICE as _PIPER_FALLBACK_VOICE,
    PIPER_VOICE_MODELS as _PIPER_VOICE_MODELS,
    VOICE_CATALOGS,
    catalog_voice_display_label as _catalog_voice_display_label,
)
from plugin.framework import config

__all__ = [
    "clean_provider_name",
    "clean_voice_name",
    "endpoint_is_together",
    "get_config",
    "get_config_str",
    "get_default_voice_for_locale",
    "get_scoped_tts_voice",
    "get_voice_catalog",
    "get_voice_family",
    "is_kokoro_voice_id",
    "kokoro_g2p_lang",
    "parse_tts_speed",
    "set_config",
    "set_scoped_tts_voice",
    "settings_voice_options",
    "tts_test_sample",
    "voice_choice_to_id",
    "voice_options_for_provider",
    "_endpoint_provider",
    "_resolve_tts_voice",
]


def get_config(*args: Any, **kwargs: Any) -> Any:
    return config.get_config(*args, **kwargs)


def get_config_str(*args: Any, **kwargs: Any) -> Any:
    return config.get_config_str(*args, **kwargs)


def set_config(*args: Any, **kwargs: Any) -> Any:
    return config.set_config(*args, **kwargs)


from plugin.framework import i18n
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

# Single source of truth for Kokoro voice prefixes and their G2P language codes
KOKORO_VOICE_PREFIX_TO_LANG: dict[str, str] = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "zh",
}

_KOKORO_PREFIX_CHARS = "".join(sorted(KOKORO_VOICE_PREFIX_TO_LANG.keys()))
_KOKORO_VOICE_ID_RE = re.compile(rf"^[{_KOKORO_PREFIX_CHARS}][fm]_", re.IGNORECASE)


def is_kokoro_voice_id(voice: str) -> bool:
    """True if voice starts with a known Kokoro prefix (e.g. af_, jf_, bm_)."""
    return bool(_KOKORO_VOICE_ID_RE.match((voice or "").strip()))


def _kokoro_lang_for_voice(voice: str) -> str:
    """Determine the Kokoro phonemizer language code from voice prefix."""
    clean = (voice or "").lower().strip()
    prefix = clean[:1]
    return KOKORO_VOICE_PREFIX_TO_LANG.get(prefix, "en-us")


# English source for Settings → Speech → Test voice. ``_()`` speaks the UI locale.
TTS_TEST_SAMPLE = "Hello, I'm your LibreOffice WriterAgent."


def tts_test_sample() -> str:
    """Localized sample sentence for Settings → Speech → Test voice."""
    return _("Hello, I'm your LibreOffice WriterAgent.")


def _text_is_latin_script(text: str) -> bool:
    """True when all alphabetic characters fall in basic Latin through Latin Extended-B."""
    return all(ord(char) < 0x0250 or not char.isalpha() for char in text)


def kokoro_g2p_lang(text: str, voice: str = "") -> str:
    """Phonemizer code for one Kokoro utterance. The voice id is not changed."""
    if not voice:
        return "en-us"
    target = _kokoro_lang_for_voice(voice)
    if target in ("en-us", "en-gb"):
        return target
    if target in ("ja", "zh", "hi") and _text_is_latin_script(text):
        return "en-us"
    return target


def _normalize_voice_family(family: str) -> str:
    """Translate legacy or UI family names into canonical catalog keys."""
    fam = (family or "").strip().lower()
    if fam in ("local", "system_speech", "os", "native"):
        return "system"
    if fam in ("remote", "api", "cloud"):
        return "openai"
    return fam


def _locale_language_stem(locale: str | None) -> str:
    """Extract language prefix from locale (``en_US`` / ``en-US`` → ``en``)."""
    clean = (locale or "").strip().replace("-", "_").lower()
    return clean.split("_")[0] if clean else "en"


def _voice_lang_matches_ui(ui_stem: str, voice_lang: str) -> bool:
    """True if a voice language matches the active UI language stem."""
    v = (voice_lang or "").strip().lower()
    if not v:
        return False
    v_stem = v.split("_")[0].split("-")[0]
    if v_stem == ui_stem:
        return True
    if ui_stem in ("nb", "nn") and v_stem == "no":
        return True
    if ui_stem == "hr" and v_stem == "sl":
        return True
    return False


def _label_says_female(label: str) -> bool:
    low = (label or "").lower()
    return "female" in low and "male" not in low.replace("female", "")


def _kokoro_voice_is_female(voice_id: str, label: str) -> bool:
    """Kokoro ids encode gender in the second letter (``af_``, ``jf_``, ``ef_``)."""
    vid = (voice_id or "").lower()
    if len(vid) >= 3 and vid[1] == "f" and vid[2] == "_":
        return True
    return _label_says_female(label)


def _piper_default_for_stem(stem: str) -> str:
    """First female voice for the language stem in Piper's catalog."""
    if stem == "en":
        return _PIPER_FALLBACK_VOICE
    any_voice = ""
    for voice_id, model in _PIPER_VOICE_MODELS.items():
        v_stem = model[2].split("_")[0].lower()
        if _voice_lang_matches_ui(stem, v_stem):
            label = model[3]
            if _label_says_female(label):
                return voice_id
            if not any_voice:
                any_voice = voice_id
    return any_voice or _PIPER_FALLBACK_VOICE


def _kokoro_default_for_stem(stem: str) -> str:
    """Default Kokoro voice for the UI language stem (prefers female)."""
    if stem == "en":
        return _KOKORO_FALLBACK_VOICE
    if stem in _LOCALE_TO_KOKORO_DEFAULT:
        return _LOCALE_TO_KOKORO_DEFAULT[stem]
    any_voice = ""
    for opt in _KOKORO_CATALOG_ITEMS:
        if opt.get("lang") == stem:
            if _kokoro_voice_is_female(opt["value"], opt["label"]):
                return opt["value"]
            if not any_voice:
                any_voice = opt["value"]
    return any_voice or _KOKORO_FALLBACK_VOICE


def get_default_voice_for_locale(family: str, locale: str | None = None) -> str:
    """Return the default voice for a family and the LibreOffice UI locale."""
    fam = _normalize_voice_family(family)
    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale

            locale = get_active_locale()
        except Exception:
            locale = "en_US"
    stem = _locale_language_stem(locale)
    if fam == "piper":
        return _piper_default_for_stem(stem)
    if fam == "kokoro":
        return _kokoro_default_for_stem(stem)
    if fam in ("openai", "endpoint"):
        return DEFAULT_VOICE_FOR_FAMILY["openai"]
    if fam in ("openrouter", "together"):
        return ""
    return DEFAULT_VOICE_FOR_FAMILY["system"]


def get_voice_catalog(family: str, locale: str | None = None) -> list[dict[str, str]]:
    """Return ordered voice options for the family, prioritizing the active locale."""
    fam = _normalize_voice_family(family)
    if fam not in ("piper", "kokoro"):
        return VOICE_CATALOGS.get(fam, [])

    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale

            locale = get_active_locale()
        except Exception:
            locale = "en_US"

    stem = _locale_language_stem(locale)

    if fam == "kokoro":
        locale_voices: list[dict[str, str]] = []
        en_voices: list[dict[str, str]] = []
        other_voices: list[dict[str, str]] = []
        for opt in _KOKORO_CATALOG_ITEMS:
            v_lang = opt.get("lang", "en")
            item = {"value": opt["value"], "label": _catalog_voice_display_label(opt["label"])}
            if stem != "en" and v_lang == stem:
                locale_voices.append(item)
            elif v_lang == "en":
                en_voices.append(item)
            else:
                other_voices.append(item)
        return locale_voices + en_voices + other_voices

    # Piper: matching locale voices first, then English, then others
    locale_voices_p: list[dict[str, str]] = []
    en_voices_p: list[dict[str, str]] = []
    other_voices_p: list[dict[str, str]] = []

    preferred_default = _piper_default_for_stem(stem)

    for voice_id, model in _PIPER_VOICE_MODELS.items():
        lang_code = model[2]
        label = model[3]
        v_stem = lang_code.split("_")[0].lower()
        item = {"value": voice_id, "label": _catalog_voice_display_label(label)}
        is_loc = stem != "en" and (
            voice_id == preferred_default or _voice_lang_matches_ui(stem, v_stem)
        )
        if is_loc:
            if voice_id == preferred_default:
                locale_voices_p.insert(0, item)
            else:
                locale_voices_p.append(item)
        elif v_stem == "en":
            en_voices_p.append(item)
        else:
            other_voices_p.append(item)

    return locale_voices_p + en_voices_p + other_voices_p


def settings_voice_options(services: Any = None) -> list[dict[str, str]]:
    """Dynamic options provider for Settings dialog's audio.tts_voice combobox."""
    try:
        provider = str(get_config("audio.tts_provider") or "system")
        model = str(get_config("audio.tts_model") or "")
        return voice_options_for_provider(provider, model)
    except Exception as exc:
        log.warning("Could not determine dynamic TTS voice options: %s", exc)
        return get_voice_catalog("system")


def endpoint_is_together(endpoint: str | None = None) -> bool:
    """True when the endpoint URL points at Together AI."""
    ep = endpoint
    if not ep:
        from plugin.framework.config import get_current_endpoint

        ep = get_current_endpoint() or ""
    return "api.together" in str(ep).lower()


def _endpoint_provider(endpoint: str | None) -> str:
    """Identify host for logging and family dispatch."""
    ep = (endpoint or "").lower()
    if "openrouter.ai" in ep:
        return "openrouter"
    if "api.together" in ep:
        return "together"
    return "generic"


def _sort_voice_rows_by_label(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Sort voice option dicts by localized display label."""
    return sorted(rows, key=lambda row: row.get("label", "").casefold())


def voice_options_for_provider(
    provider: str,
    model: str | None = None,
    locale: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> list[dict[str, str]]:
    """Voice rows for Settings.

    Together + LLM Endpoint reads ``cached_tts_supported_voices`` for
    that TTS model. A miss does not HTTP: the Settings worker fills the
    cache before the dialog applies it. OpenRouter models with a harvested
    ``supported_voices`` list use those ids.
    An OpenRouter speech model whose list omitted voices, or whose speech list
    has not been fetched yet, returns ``[]`` so the combo stays free text
    instead of the OpenAI alloy list. Other OpenAI-compatible endpoints keep
    the openai catalog. Remote endpoint rows are sorted by display label.
    Local Kokoro and Piper, including Kokoro served by an endpoint model id,
    keep catalog and locale order.
    """
    prov = clean_provider_name(provider)
    if prov == "endpoint":
        together_rows = _together_endpoint_voice_rows(str(model or ""), endpoint, api_key)
        if together_rows is not None:
            return _sort_voice_rows_by_label(together_rows)
        rows = _endpoint_voice_rows(str(model or ""))
        if rows is not None:
            return _sort_voice_rows_by_label(rows)
        family = get_voice_family(prov, model, endpoint)
        catalog = get_voice_catalog(family, locale)
        # Alloy/nova list for other OpenAI-compatible hosts. Endpoint Kokoro
        # keeps locale order. Together returns above (rows or []).
        if family == "openai":
            return _sort_voice_rows_by_label(catalog)
        return catalog
    return get_voice_catalog(get_voice_family(prov, model, endpoint), locale)


def _together_endpoint_voice_rows(
    model: str,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> list[dict[str, str]] | None:
    """Together voice rows, ``[]`` so alloy is not shown, or None for the family catalog.

    Kokoro with no Together answer falls through to the local Kokoro catalog.
    Orpheus and Cartesia do not: an empty list beats the OpenAI alloy list.
    """
    del api_key
    if not endpoint_is_together(endpoint):
        return None
    mid = (model or "").strip()
    if not mid:
        return []
    from plugin.framework.client.model_fetcher import cached_tts_supported_voices

    voices = cached_tts_supported_voices(mid)
    if voices:
        # Label equals the token /audio/speech must send (Cartesia id or voice name).
        return [{"value": voice, "label": voice} for voice in voices]
    if "kokoro" in mid.lower():
        return None
    return []


def _endpoint_voice_rows(model: str) -> list[dict[str, str]] | None:
    """OR voice rows, ``[]`` for free text, or None to use the family catalog."""
    from plugin.framework.client.model_fetcher import (
        cached_tts_supported_voices,
        openrouter_speech_list_has_model,
        openrouter_speech_list_loaded,
    )

    voices = cached_tts_supported_voices(model) if model else []
    if voices:
        # Label equals the API id. Pretty-casing would hide ids the request must send.
        return [{"value": voice, "label": voice} for voice in voices]
    if model and "kokoro" in model.lower():
        return None
    if model and openrouter_speech_list_has_model(model):
        # Fetched speech row with no supported_voices: do not invent alloy.
        return []
    if _saved_endpoint_is_openrouter() and not openrouter_speech_list_loaded():
        # Not fetched yet. Alloy is the wrong list for Grok/Gemini speech models.
        return []
    return None


def _saved_endpoint_is_openrouter() -> bool:
    """True when the saved chat endpoint is OpenRouter.

    Speech uses the LLM Endpoint provider, so the host that will speak is the
    saved URL, not an unsaved value still sitting in the endpoint combo.
    """
    try:
        from plugin.framework.client.provider_detection import get_provider_from_endpoint
        from plugin.framework.config import get_current_endpoint as saved_endpoint

        return get_provider_from_endpoint(saved_endpoint() or "") == "openrouter"
    except Exception:
        log.debug("TTS voice list: endpoint provider unavailable", exc_info=True)
        return False


def clean_provider_name(provider_or_label: str) -> str:
    """Extract canonical provider code from a provider string or UI label."""
    if not provider_or_label:
        return "system"
    clean = provider_or_label.split(" (")[0].strip().lower()
    for code in ("endpoint", "kokoro", "piper", "system"):
        if code in clean:
            return code
    return "system"


def clean_voice_name(voice_or_label: str) -> str:
    """Extract canonical voice code from a voice string or legacy UI label.

    ``af_bella (Kokoro US Female - Bella)`` yields ``af_bella``.
    Does not break file paths containing parentheses like
    ``C:\\Program Files (x86)\\...``.
    """
    if not voice_or_label:
        return ""
    text = voice_or_label.strip()
    if os.path.isabs(text) and not text.endswith(")"):
        return text
    # Only strip a trailing parenthesized label
    m = re.match(r"^(.*?)\s+\([^()]+\)$", text)
    if m:
        candidate = m.group(1).strip()
        if candidate:
            return candidate
    return text


def voice_choice_to_id(choice: str, options: list[dict[str, str]] | None = None) -> str:
    """Map a Voice combo string to the stored voice id."""
    text = (choice or "").strip()
    if not text:
        return ""
    rows = options or []
    for opt in rows:
        if text == str(opt.get("value") or ""):
            return text
    for opt in rows:
        label = str(opt.get("label") or "")
        if text == label or (label and text == i18n._(label)):
            return str(opt.get("value") or "")
    return clean_voice_name(text)


def get_voice_family(
    provider: str | None,
    model: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Return voice family key ('kokoro', 'piper', 'openai', 'openrouter', 'together', 'system')."""
    prov = clean_provider_name(provider or "")
    if prov == "kokoro":
        return "kokoro"
    if prov == "piper":
        return "piper"
    if prov == "endpoint":
        if model and "kokoro" in model.lower():
            return "kokoro"
        if endpoint_is_together(endpoint):
            return "together"
        if model and _endpoint_uses_openrouter_voices(model):
            return "openrouter"
        return "openai"
    return "system"


def _endpoint_uses_openrouter_voices(model: str) -> bool:
    """True when this endpoint model should not use the OpenAI voice family."""
    from plugin.framework.client.model_fetcher import (
        cached_tts_supported_voices,
        openrouter_speech_list_has_model,
    )

    if cached_tts_supported_voices(model):
        return True
    return openrouter_speech_list_has_model(model)


def _preferred_harvested_voice(model: str | None, voices: list[str]) -> str:
    """First choice from an advertised voice array."""
    if not voices:
        return ""
    m = (model or "").lower()
    if "gemini" in m:
        for candidate in ("Aoede", "aoede"):
            if candidate in voices:
                return candidate
    sorted_ids = sorted(voices, key=lambda vid: vid.casefold())
    return sorted_ids[0]


def _voice_belongs_to_other_family(voice: str, current_family: str) -> bool:
    """True if voice is known to belong exclusively to another family."""
    if not voice:
        return False
    clean_v = clean_voice_name(voice)
    for fam in ("kokoro", "piper", "openai", "system"):
        if fam == current_family:
            continue
        fam_voices = {opt["value"] for opt in get_voice_catalog(fam)}
        if clean_v in fam_voices:
            return True
    if current_family not in ("kokoro",):
        if clean_v in _KOKORO_VOICES or is_kokoro_voice_id(clean_v):
            return True
    return False


def get_scoped_tts_voice(
    provider: str | None = None,
    model: str | None = None,
    locale: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Get the scoped voice for the given provider/model's voice family."""
    if provider is None:
        provider = str(get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        try:
            from plugin.framework.client.model_fetcher import get_tts_model

            model = get_tts_model()
        except ImportError:
            model = None

    family = get_voice_family(prov_clean, model, endpoint)
    scoped_key = f"audio.tts_voice_{family}"
    val = get_config(scoped_key)
    clean_scoped = clean_voice_name(val.strip()) if isinstance(val, str) and val.strip() else ""
    general_voice = str(get_config("audio.tts_voice") or "").strip()
    clean_gen = clean_voice_name(general_voice)

    voices: list[str] = []
    if prov_clean == "endpoint" and model:
        from plugin.framework.client.model_fetcher import cached_tts_supported_voices

        voices = cached_tts_supported_voices(str(model)) or []
    if voices:
        if clean_scoped in voices:
            return clean_scoped
        if clean_gen in voices:
            return clean_gen
        return _preferred_harvested_voice(model, voices)

    if clean_scoped:
        return clean_scoped

    if family in ("openrouter", "together"):
        if clean_gen and not _voice_belongs_to_other_family(clean_gen, family):
            return clean_gen
        return get_default_voice_for_locale(family, locale)

    valid_voices = {opt["value"] for opt in get_voice_catalog(family, locale)}
    if clean_gen in valid_voices:
        return clean_gen

    return get_default_voice_for_locale(family, locale)


def set_scoped_tts_voice(
    voice: str,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> None:
    """Persist the voice selection for the given provider/model family."""
    clean_v = clean_voice_name(voice)
    if not clean_v:
        return
    family = get_voice_family(provider, model, endpoint)
    scoped_key = f"audio.tts_voice_{family}"
    set_config(scoped_key, clean_v)
    set_config("audio.tts_voice", clean_v)
    log.info("Saved TTS voice '%s' (scoped to %s)", clean_v, scoped_key)


def parse_tts_speed(val: Any) -> float:
    """Parse speech speed safely, enforcing a minimum of 0.25x."""
    if val is None or val == "":
        return 1.0
    if isinstance(val, (int, float)):
        return max(0.25, min(5.0, float(val)))
    cleaned = str(val).split("(")[0].strip().rstrip("xX").strip().replace(",", ".")
    try:
        speed = float(cleaned)
        return max(0.25, min(5.0, speed))
    except (ValueError, TypeError):
        return 1.0


def _resolve_tts_voice(model: str, voice: str) -> str:
    """Ensure voice name is compatible with the target model."""
    voice = clean_voice_name(voice)
    if not voice:
        voice = "alloy"
    if "kokoro" in model.lower():
        v_low = voice.lower()
        if is_kokoro_voice_id(v_low):
            return voice
        return _KOKORO_VOICES.get(v_low, _KOKORO_FALLBACK_VOICE)
    return voice
