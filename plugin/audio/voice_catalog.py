# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the TTS voice catalog once from ``data/voice_catalog.json``.

Piper/Kokoro/OpenAI/OS voice lists, locale defaults, and the OpenAI→Kokoro
alias map used to be inline dicts in ``tts_service``. Settings and the
sidebar both read this asset so those lists cannot drift apart. JSON (not
YAML) because the LibreOffice runtime does not ship PyYAML.

_parse_catalog validates ids, fallbacks, locale defaults and aliases at import,
so a broken asset fails loudly instead of half-loading.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any

# onnx relative path, config relative path, lang code, display label.
PiperModel = tuple[str, str, str, str]


@dataclass(frozen=True)
class VoiceRow:
    id: str
    label: str
    lang: str = "en"
    short_name: str = ""


@dataclass(frozen=True)
class PiperRow:
    id: str
    onnx: str
    config: str
    lang: str
    label: str
    short_name: str = ""


@dataclass(frozen=True)
class Catalog:
    piper: tuple[PiperRow, ...]
    kokoro: tuple[VoiceRow, ...]
    openai: tuple[VoiceRow, ...]
    system: tuple[VoiceRow, ...]
    kokoro_locale_defaults: dict[str, str]
    kokoro_aliases: dict[str, str]
    piper_fallback: str
    kokoro_fallback: str
    openai_fallback: str
    system_fallback: str


CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "voice_catalog.json")

_FALLBACK_DEFAULTS: dict[str, str] = {
    "piper": "en_US-lessac-medium",
    "kokoro": "af_sky",
    "openai": "alloy",
    "system": "default",
}


def _load_catalog(path: str = CATALOG_PATH) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_catalog(raw: Any) -> Catalog:
    if not isinstance(raw, dict):
        raise ValueError("voice_catalog.json: asset must be a JSON object")

    parsed_piper: list[PiperRow] = []
    parsed_kokoro: list[VoiceRow] = []
    parsed_openai: list[VoiceRow] = []
    parsed_system: list[VoiceRow] = []

    family_rows: dict[str, list[Any]] = {
        "piper": parsed_piper,
        "kokoro": parsed_kokoro,
        "openai": parsed_openai,
        "system": parsed_system,
    }

    family_ids: dict[str, set[str]] = {
        "piper": set(),
        "kokoro": set(),
        "openai": set(),
        "system": set(),
    }

    fallbacks: dict[str, str] = {}
    kokoro_locale_defaults: dict[str, str] = {}

    for family in ("piper", "kokoro", "openai", "system"):
        block = raw.get(family)
        if not isinstance(block, dict):
            raise ValueError(f"voice_catalog.json: missing '{family}' object")

        voices = block.get("voices")
        if not isinstance(voices, list):
            raise ValueError(f"voice_catalog.json: {family}.voices must be a list")

        seen_ids = family_ids[family]
        rows = family_rows[family]

        for idx, entry in enumerate(voices):
            if not isinstance(entry, dict):
                raise ValueError(f"voice_catalog.json: {family}.voices[{idx}] must be an object")

            if family == "piper":
                req_fields = ("id", "onnx", "config", "lang", "label")
            else:
                req_fields = ("id", "label")

            for field in req_fields:
                if field not in entry:
                    raise ValueError(f"voice_catalog.json: {family}.voices[{idx}] missing required field '{field}'")
                val = entry[field]
                if not isinstance(val, str) or not val:
                    raise ValueError(f"voice_catalog.json: {family}.voices[{idx}] field '{field}' must be a non-empty string")

            voice_id = entry["id"]
            if voice_id in seen_ids:
                raise ValueError(f"voice_catalog.json: {family}.voices[{idx}] duplicate id '{voice_id}'")
            seen_ids.add(voice_id)

            short_raw = entry.get("short_name")
            short_name = short_raw if isinstance(short_raw, str) else ""

            if family == "piper":
                rows.append(
                    PiperRow(
                        id=voice_id,
                        onnx=entry["onnx"],
                        config=entry["config"],
                        lang=entry["lang"],
                        label=entry["label"],
                        short_name=short_name,
                    )
                )
            else:
                lang_raw = entry.get("lang")
                lang = lang_raw if isinstance(lang_raw, str) and lang_raw else "en"
                rows.append(
                    VoiceRow(
                        id=voice_id,
                        label=entry["label"],
                        lang=lang,
                        short_name=short_name,
                    )
                )

        raw_fallback = block.get("fallback_voice")
        fallback_val = _FALLBACK_DEFAULTS[family] if raw_fallback is None else raw_fallback
        if not isinstance(fallback_val, str) or not fallback_val or fallback_val not in seen_ids:
            raise ValueError(f"voice_catalog.json: {family}.fallback_voice -> '{fallback_val}' is not a {family} voice")
        fallbacks[family] = fallback_val

        loc_defaults = block.get("locale_defaults")
        if loc_defaults is not None:
            if not isinstance(loc_defaults, dict):
                raise ValueError(f"voice_catalog.json: {family}.locale_defaults must be an object")
            for loc_key, target_id in loc_defaults.items():
                if not isinstance(loc_key, str) or not loc_key or not isinstance(target_id, str) or target_id not in seen_ids:
                    raise ValueError(
                        f"voice_catalog.json: {family}.locale_defaults['{loc_key}'] -> '{target_id}' is not a {family} voice"
                    )
            if family == "kokoro":
                kokoro_locale_defaults = dict(loc_defaults)

    kokoro_block = raw["kokoro"]
    aliases = kokoro_block.get("openai_aliases")
    if not isinstance(aliases, dict):
        raise ValueError("voice_catalog.json: kokoro.openai_aliases must be an object")

    kokoro_aliases: dict[str, str] = {}
    for alias_key, target_id in aliases.items():
        if not isinstance(alias_key, str) or not alias_key or not isinstance(target_id, str) or target_id not in family_ids["kokoro"]:
            raise ValueError(
                f"voice_catalog.json: kokoro.openai_aliases.{alias_key} -> '{target_id}' is not a kokoro voice"
            )
        kokoro_aliases[alias_key] = target_id

    return Catalog(
        piper=tuple(parsed_piper),
        kokoro=tuple(parsed_kokoro),
        openai=tuple(parsed_openai),
        system=tuple(parsed_system),
        kokoro_locale_defaults=kokoro_locale_defaults,
        kokoro_aliases=kokoro_aliases,
        piper_fallback=fallbacks["piper"],
        kokoro_fallback=fallbacks["kokoro"],
        openai_fallback=fallbacks["openai"],
        system_fallback=fallbacks["system"],
    )


_CATALOG = _parse_catalog(_load_catalog())
PIPER_VOICE_MODELS: dict[str, PiperModel] = {
    r.id: (r.onnx, r.config, r.lang, r.label) for r in _CATALOG.piper
}
KOKORO_CATALOG_ITEMS: list[dict[str, str]] = [
    {"value": r.id, "label": r.label, "lang": r.lang} for r in _CATALOG.kokoro
]
LOCALE_TO_KOKORO_DEFAULT = dict(_CATALOG.kokoro_locale_defaults)
PIPER_FALLBACK_VOICE = _CATALOG.piper_fallback
KOKORO_FALLBACK_VOICE = _CATALOG.kokoro_fallback
KOKORO_OPENAI_ALIASES = dict(_CATALOG.kokoro_aliases)
VOICE_CATALOGS = {
    "kokoro": [{"value": r.id, "label": r.label} for r in _CATALOG.kokoro],
    "piper": [{"value": r.id, "label": r.label} for r in _CATALOG.piper],
    "openai": [{"value": r.id, "label": r.label} for r in _CATALOG.openai],
    "system": [{"value": r.id, "label": r.label} for r in _CATALOG.system],
}
DEFAULT_VOICE_FOR_FAMILY = {
    "kokoro": KOKORO_FALLBACK_VOICE,
    "piper": PIPER_FALLBACK_VOICE,
    "openai": _CATALOG.openai_fallback,
    "system": _CATALOG.system_fallback,
}

_SHORT_NAMES: dict[str, str] = {
    r.id: r.short_name for r in _CATALOG.piper + _CATALOG.kokoro if r.short_name
}


def voice_short_name(voice_id: str) -> str:
    """Human name from the catalog, e.g. Thorsten for ``de_DE-thorsten-medium``."""
    # Deliberately uses first segment before ' (' rather than a trailing paren group
    clean = (voice_id or "").split(" (")[0].strip()
    named = _SHORT_NAMES.get(clean)
    if named:
        return named
    return clean or voice_id


__all__ = [
    "CATALOG_PATH",
    "DEFAULT_VOICE_FOR_FAMILY",
    "KOKORO_CATALOG_ITEMS",
    "KOKORO_FALLBACK_VOICE",
    "KOKORO_OPENAI_ALIASES",
    "LOCALE_TO_KOKORO_DEFAULT",
    "PIPER_FALLBACK_VOICE",
    "PIPER_VOICE_MODELS",
    "VOICE_CATALOGS",
    "voice_short_name",
]
