# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""TTS voice catalog, scoped voice resolution, and voice settings helpers."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from plugin.audio.kokoro_g2p import KOKORO_VOICE_PREFIX_TO_LANG
from plugin.audio.voice_catalog import (
    DEFAULT_VOICE_FOR_FAMILY,
    KOKORO_CATALOG_ITEMS as _KOKORO_CATALOG_ITEMS,
    KOKORO_FALLBACK_VOICE as _KOKORO_FALLBACK_VOICE,
    KOKORO_OPENAI_ALIASES as _KOKORO_OPENAI_ALIASES,
    LOCALE_TO_KOKORO_DEFAULT as _LOCALE_TO_KOKORO_DEFAULT,
    LOCALE_TO_PIPER_DEFAULT as _LOCALE_TO_PIPER_DEFAULT,
    PIPER_FALLBACK_VOICE as _PIPER_FALLBACK_VOICE,
    PIPER_VOICE_MODELS as _PIPER_VOICE_MODELS,
    VOICE_CATALOGS,
)
from plugin.framework import config, i18n
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

__all__ = [
    "VoiceContext",
    "canonical_voice_id",
    "catalog_voice_display_label",
    "clean_provider_name",
    "clean_voice_name",
    "get_default_voice_for_locale",
    "get_scoped_tts_voice",
    "get_voice_catalog",
    "get_voice_family",
    "is_kokoro_voice_id",
    "match_voice_id",
    "parse_tts_speed",
    "pick_voice_id",
    "resolve_voice_context",
    "set_scoped_tts_voice",
    "settings_voice_options",
    "tts_test_sample",
    "voice_choice_to_id",
    "voice_options_for_provider",
]


@dataclass(frozen=True)
class VoiceContext:
    family: str                 # kokoro|piper|openai|openrouter|together|system
    source: str                 # "catalog"|"harvested"|"unknown"
    rows: tuple[dict[str, str], ...]
    editable: bool


def is_kokoro_voice_id(voice: str) -> bool:
    """True if voice starts with a known Kokoro prefix (e.g. af_, jf_, bm_)."""
    clean = (voice or "").strip().lower()
    return len(clean) >= 3 and clean[0] in KOKORO_VOICE_PREFIX_TO_LANG and clean[1:3] in ("f_", "m_")


# English source for Settings → Speech → Test voice. ``_()`` speaks the UI locale.
TTS_TEST_SAMPLE = "Hello, I'm your LibreOffice WriterAgent."


def tts_test_sample() -> str:
    """Localized sample sentence for Settings → Speech → Test voice."""
    return _("Hello, I'm your LibreOffice WriterAgent.")


def clean_provider_name(provider_or_label: str) -> str:
    """Extract canonical provider code from a provider string or UI label."""
    if not provider_or_label:
        return "system"
    # Deliberately uses first segment before ' (' to inspect the provider keyword
    clean = provider_or_label.split(" (")[0].strip().lower()
    for code in ("endpoint", "kokoro", "piper", "system"):
        if code in clean:
            return code
    return "system"


def _normalize_voice_family(family: str) -> str:
    """Translate legacy or UI family names or provider labels into canonical catalog keys."""
    fam = (family or "").strip().lower()
    if not fam:
        return "system"
    if fam in ("local", "system_speech", "os", "native"):
        return "system"
    if fam in ("remote", "api", "cloud"):
        return "openai"
    if fam in ("kokoro", "piper", "openai", "openrouter", "together", "system"):
        return fam
    if "openrouter" in fam:
        return "openrouter"
    if "together" in fam:
        return "together"
    if "openai" in fam:
        return "openai"
    return clean_provider_name(family)


def _locale_language_stem(locale: str | None) -> str:
    """Extract language prefix from locale (``en_US`` / ``en-US`` → ``en``)."""
    clean = (locale or "").strip().replace("-", "_").lower()
    return clean.split("_")[0] if clean else "en"


def _voice_lang_matches_ui(ui_stem: str, voice_lang: str) -> bool:
    """True if a voice language matches the active UI language stem."""
    if not voice_lang:
        return False
    v_stem = _locale_language_stem(voice_lang)
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


def _first_matching(
    rows: Any,
    is_match: Any,
    is_preferred: Any,
    fallback: str,
) -> str:
    """First preferred match, else the first match, else fallback."""
    any_voice = ""
    for item in rows:
        if is_match(item):
            voice_id = item["value"] if isinstance(item, dict) else item[0]
            if is_preferred(item):
                return voice_id
            if not any_voice:
                any_voice = voice_id
    return any_voice or fallback


_TRAILING_PARENS = re.compile(r"^(.*?)\s+\(([^()]+)\)$")


def split_display_label(text: str) -> tuple[str, str | None]:
    """Split a trailing parenthetical from a display label or combo string.

    Returns (head, inside) when text ends with ' (no-nested-parens)',
    otherwise (stripped_text, None).
    """
    clean = (text or "").strip()
    m = _TRAILING_PARENS.match(clean)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return clean, None


def catalog_voice_display_label(label: str) -> str:
    """Listbox text for one Piper or Kokoro catalog label.

    The provider control already names the engine, so the Voice combo shows
    only the words inside the parentheses. A leading ``Kokoro `` on that
    phrase is dropped too. ``en_US-lessac-medium (US English Female - Lessac)``
    becomes ``US English Female - Lessac``. ``af_heart (Kokoro US Female - Heart)``
    becomes ``US Female - Heart``. A string with no parentheses is an id
    (harvested voices) and is returned unchanged. The stored selection stays
    the voice id; this does not rewrite it.
    """
    head, inside = split_display_label(label)
    if inside is None:
        return head
    human = inside
    if human.casefold().startswith("kokoro "):
        human = human[len("kokoro "):].strip()
    return human or head


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _looks_like_path(text: str) -> bool:
    """True for absolute paths, Windows drive/UNC paths, or strings with / or \\."""
    if not text:
        return False
    if os.path.isabs(text) or _WIN_DRIVE_RE.match(text) or text.startswith(r"\\"):
        return True
    return "/" in text or "\\" in text


def clean_voice_name(voice_or_label: str) -> str:
    """Extract canonical voice code from a voice string or legacy UI label.

    ``af_bella (Kokoro US Female - Bella)`` yields ``af_bella``.
    Preserves paths with parentheses like ``C:\\Program Files (x86)``.
    """
    if not voice_or_label:
        return ""
    text = voice_or_label.strip()
    if _looks_like_path(text):
        head, inside = split_display_label(text)
        if inside is not None and head and _FILE_EXT_RE.search(head):
            return head
        return text
    head, inside = split_display_label(text)
    if inside is not None and head:
        return head
    return text


def _buckets(
    rows: Any,
    is_locale: Any,
    is_english: Any,
    preferred_id: str | None = None,
) -> list[dict[str, str]]:
    """locale + english + other. preferred_id is inserted at the front of locale."""
    locale_voices: list[dict[str, str]] = []
    en_voices: list[dict[str, str]] = []
    other_voices: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            item = {"value": row["value"], "label": catalog_voice_display_label(row["label"])}
            vid = row["value"]
        else:
            vid, model = row
            item = {"value": vid, "label": catalog_voice_display_label(model[3])}
        if is_locale(row):
            if preferred_id and vid == preferred_id:
                locale_voices.insert(0, item)
            else:
                locale_voices.append(item)
        elif is_english(row):
            en_voices.append(item)
        else:
            other_voices.append(item)
    return locale_voices + en_voices + other_voices


def _piper_default_for_stem(stem: str) -> str:
    """Default Piper voice for the UI language stem (prefers JSON map, else female-first)."""
    if stem in _LOCALE_TO_PIPER_DEFAULT:
        return _LOCALE_TO_PIPER_DEFAULT[stem]
    return _first_matching(
        _PIPER_VOICE_MODELS.items(),
        lambda item: _voice_lang_matches_ui(stem, item[1][2]),
        lambda item: _label_says_female(item[1][3]),
        _PIPER_FALLBACK_VOICE,
    )


def _kokoro_default_for_stem(stem: str) -> str:
    """Default Kokoro voice for the UI language stem (prefers female)."""
    if stem == "en":
        return _KOKORO_FALLBACK_VOICE
    if stem in _LOCALE_TO_KOKORO_DEFAULT:
        return _LOCALE_TO_KOKORO_DEFAULT[stem]
    return _first_matching(
        _KOKORO_CATALOG_ITEMS,
        lambda opt: opt.get("lang") == stem,
        lambda opt: _kokoro_voice_is_female(opt["value"], opt["label"]),
        _KOKORO_FALLBACK_VOICE,
    )


def get_default_voice_for_locale(family: str, locale: str | None = None) -> str:
    """Return default voice for a family (canonical key or provider label) and UI locale."""
    fam = _normalize_voice_family(family)
    if locale is None:
        locale = i18n.get_active_locale()
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
    """Return ordered voice options for a family (canonical key or provider label), prioritizing locale."""
    fam = _normalize_voice_family(family)
    if fam not in ("piper", "kokoro"):
        return VOICE_CATALOGS.get(fam, [])

    if locale is None:
        locale = i18n.get_active_locale()

    stem = _locale_language_stem(locale)

    if fam == "kokoro":
        return _buckets(
            _KOKORO_CATALOG_ITEMS,
            is_locale=lambda opt: stem != "en" and opt.get("lang") == stem,
            is_english=lambda opt: opt.get("lang") == "en",
        )

    preferred_default = _piper_default_for_stem(stem)
    return _buckets(
        _PIPER_VOICE_MODELS.items(),
        is_locale=lambda item: stem != "en" and (
            item[0] == preferred_default or _voice_lang_matches_ui(stem, item[1][2])
        ),
        is_english=lambda item: _locale_language_stem(item[1][2]) == "en",
        preferred_id=preferred_default,
    )


def settings_voice_options(_services: Any = None) -> list[dict[str, str]]:
    """Dynamic options provider for Settings dialog's audio.tts_voice combobox."""
    try:
        provider = str(config.get_config("audio.tts_provider") or "system")
        model = str(config.get_config("audio.tts_model") or "")
        return voice_options_for_provider(provider, model)
    except Exception as exc:
        log.warning("Could not determine dynamic TTS voice options: %s", exc)
        return get_voice_catalog("system")


def _sort_voice_rows_by_label(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Sort voice option dicts by localized display label."""
    return sorted(rows, key=lambda row: row.get("label", "").casefold())


def match_voice_id(voice: str, candidates: Iterable[str]) -> str | None:
    """Return matching candidate: exact match first, then first casefold match, else None."""
    v = (voice or "").strip()
    if not v:
        return None
    cand_list = list(candidates)
    for c in cand_list:
        if c == v:
            return c
    v_fold = v.casefold()
    for c in cand_list:
        if c.casefold() == v_fold:
            return c
    return None


def canonical_voice_id(family: str, voice: str) -> str:
    """Canonicalize a voice id: clean name, match catalog spelling case-insensitively.

    Returns paths and unknown free text unchanged, lowercases known Kokoro ids,
    and maps Kokoro OpenAI aliases case-insensitively.
    """
    clean_v = clean_voice_name(voice)
    if not clean_v:
        return ""
    if _looks_like_path(clean_v):
        return clean_v

    fam = _normalize_voice_family(family)
    if fam == "kokoro":
        v_low = clean_v.lower()
        alias_target = match_voice_id(v_low, _KOKORO_OPENAI_ALIASES)
        if alias_target is not None:
            clean_v = _KOKORO_OPENAI_ALIASES[alias_target]
        elif is_kokoro_voice_id(v_low):
            clean_v = v_low

    if fam in ("kokoro", "piper", "openai", "system"):
        catalog_values = [opt["value"] for opt in get_voice_catalog(fam)]
        matched = match_voice_id(clean_v, catalog_values)
        if matched is not None:
            return matched
        if fam == "kokoro" and is_kokoro_voice_id(clean_v):
            return clean_v.lower()

    return clean_v


def _preferred_harvested_voice(model: str | None, voices: Sequence[str]) -> str:
    """First choice from an advertised voice array."""
    if not voices:
        return ""
    m = (model or "").lower()
    if "gemini" in m:
        matched = match_voice_id("Aoede", voices)
        if matched is not None:
            return matched
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
        fam_voices = [opt["value"] for opt in get_voice_catalog(fam)]
        if match_voice_id(clean_v, fam_voices) is not None:
            return True
    if current_family not in ("kokoro",):
        if match_voice_id(clean_v, _KOKORO_OPENAI_ALIASES) is not None or is_kokoro_voice_id(clean_v):
            return True
    return False


def _catalog_context(family: str, locale: str | None = None) -> VoiceContext:
    rows = get_voice_catalog(family, locale)
    if family == "openai":
        rows = _sort_voice_rows_by_label(rows)
    return VoiceContext(family=family, source="catalog", rows=tuple(rows), editable=False)


def resolve_voice_context(
    provider: str | None,
    model: str | None = None,
    endpoint: str | None = None,
    locale: str | None = None,
) -> VoiceContext:
    """Resolve TTS voice family, source, catalog rows, and editability."""
    prov = clean_provider_name(provider or "")
    if prov in ("kokoro", "piper", "system"):
        return _catalog_context(prov, locale)

    # Provider is "endpoint"
    m_str = str(model or "").strip()
    if m_str and "kokoro" in m_str.lower():
        return _catalog_context("kokoro", locale)

    # Resolve endpoint and host
    ep_val = endpoint if (endpoint is not None and str(endpoint).strip()) else config.get_current_endpoint()
    ep_str = str(ep_val or "").strip()
    host: str | None = None
    if ep_str:
        try:
            from plugin.framework.client.provider_detection import get_provider_from_endpoint
            from plugin.framework.errors import ConfigError

            host = get_provider_from_endpoint(ep_str)
        except (ConfigError, ValueError):
            host = None

    if m_str:
        from plugin.framework.client.model_fetcher import cached_tts_supported_voices

        tokens = cached_tts_supported_voices(m_str)
        if tokens:
            fam = "together" if host == "together" else "openrouter"
            harvested_rows = [{"value": t, "label": t} for t in tokens]
            return VoiceContext(
                family=fam,
                source="harvested",
                rows=tuple(_sort_voice_rows_by_label(harvested_rows)),
                editable=False,
            )

    if host == "together":
        return VoiceContext(family="together", source="unknown", rows=(), editable=True)

    if host == "openrouter":
        from plugin.framework.client.model_fetcher import _model_fetch_tts_cache, cached_tts_models

        models = cached_tts_models(ep_str)
        if models is None:
            raw_speech = _model_fetch_tts_cache.get("speech")
            if isinstance(raw_speech, list):
                models = raw_speech

        if models is None:
            return VoiceContext(family="openrouter", source="unknown", rows=(), editable=True)
        if m_str and match_voice_id(m_str, models) is not None:
            return VoiceContext(family="openrouter", source="unknown", rows=(), editable=True)
        return _catalog_context("openai", locale)

    return _catalog_context("openai", locale)


def get_voice_family(
    provider: str | None,
    model: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Return voice family key ('kokoro', 'piper', 'openai', 'openrouter', 'together', 'system')."""
    return resolve_voice_context(provider, model, endpoint=endpoint).family


def voice_options_for_provider(
    provider: str,
    model: str | None = None,
    locale: str | None = None,
    endpoint: str | None = None,
) -> list[dict[str, str]]:
    """Voice rows for Settings."""
    return list(resolve_voice_context(provider, model, locale=locale, endpoint=endpoint).rows)


def voice_choice_to_id(choice: str, options: Sequence[dict[str, str]] | None = None) -> str:
    """Map a Voice combo string to the stored voice id."""
    text = (choice or "").strip()
    if not text:
        return ""
    rows = options or []
    values = [str(opt.get("value") or "") for opt in rows if "value" in opt]
    matched_val = match_voice_id(text, values)
    if matched_val is not None:
        return matched_val
    for opt in rows:
        label = str(opt.get("label") or "")
        if text == label or (label and text == i18n._(label)):
            return str(opt.get("value") or "")
    return clean_voice_name(text)


def pick_voice_id(
    *,
    family: str,
    model: str | None,
    options: Sequence[dict[str, str]],
    current_id: str = "",
    stored_id: str = "",
    locale: str | None = None,
) -> str:
    """Pick voice id for Settings dialog sync and save."""
    if not options:
        return match_voice_id(current_id, []) or current_id or stored_id

    values = [str(opt.get("value") or "") for opt in options if "value" in opt]
    if current_id:
        matched = match_voice_id(current_id, values)
        if matched is not None:
            return matched
    if stored_id:
        matched = match_voice_id(stored_id, values)
        if matched is not None:
            return matched
    if family in ("openrouter", "together"):
        return _preferred_harvested_voice(model, values)

    def_voice = get_default_voice_for_locale(family, locale)
    matched = match_voice_id(def_voice, values)
    if matched is not None:
        return matched
    return options[0]["value"]


def get_scoped_tts_voice(
    provider: str | None = None,
    model: str | None = None,
    locale: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Get the scoped voice for the given provider/model's voice family."""
    if provider is None:
        provider = str(config.get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        from plugin.framework.client.model_fetcher import get_tts_model

        model = get_tts_model()

    context = resolve_voice_context(prov_clean, model, endpoint=endpoint, locale=locale)
    family = context.family
    scoped_key = f"audio.tts_voice_{family}"
    val = config.get_config(scoped_key)
    clean_scoped = clean_voice_name(val.strip()) if isinstance(val, str) and val.strip() else ""
    general_voice = str(config.get_config("audio.tts_voice") or "").strip()
    clean_gen = clean_voice_name(general_voice)

    if context.source == "harvested":
        harvested_values = [opt["value"] for opt in context.rows]
        if clean_scoped:
            matched = match_voice_id(clean_scoped, harvested_values)
            if matched is not None:
                return matched
        if clean_gen:
            matched = match_voice_id(clean_gen, harvested_values)
            if matched is not None:
                return matched
        return _preferred_harvested_voice(model, harvested_values)

    if context.editable and family in ("openrouter", "together"):
        if clean_scoped and not _voice_belongs_to_other_family(clean_scoped, family):
            return clean_scoped
        if clean_gen and not _voice_belongs_to_other_family(clean_gen, family):
            return clean_gen
        return ""

    # Catalog families
    valid_voices = {opt["value"] for opt in context.rows}
    if clean_scoped:
        canon_scoped = canonical_voice_id(family, clean_scoped)
        if canon_scoped in valid_voices or _looks_like_path(canon_scoped):
            return canon_scoped
    if clean_gen:
        canon_gen = canonical_voice_id(family, clean_gen)
        if canon_gen in valid_voices or _looks_like_path(canon_gen):
            return canon_gen

    return get_default_voice_for_locale(family, locale)


def set_scoped_tts_voice(
    voice: str,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> None:
    """Persist the voice selection for the given provider/model family."""
    family = get_voice_family(provider, model, endpoint)
    clean_v = canonical_voice_id(family, voice)
    if not clean_v:
        return
    scoped_key = f"audio.tts_voice_{family}"
    config.set_config(scoped_key, clean_v)
    config.set_config("audio.tts_voice", clean_v)
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
