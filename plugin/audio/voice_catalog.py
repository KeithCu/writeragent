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

import json
import os
from typing import Any

# onnx relative path, config relative path, lang code, display label.
PiperModel = tuple[str, str, str, str]


CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "voice_catalog.json")

_PIPER_FIELDS: tuple[str, ...] = ("id", "onnx", "config", "lang", "label")
_VOICE_FIELDS: tuple[str, ...] = ("id", "label")

def _load_catalog(path: str = CATALOG_PATH) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_catalog(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("voice_catalog.json: asset must be a JSON object")

    piper_models: dict[str, PiperModel] = {}
    voice_catalogs: dict[str, list[dict[str, str]]] = {
        "openai": [],
        "system": [],
    }
    kokoro_items: list[dict[str, str]] = []
    short_names: dict[str, str] = {}
    kokoro_ids: set[str] = set()
    fallbacks: dict[str, str] = {}
    kokoro_locale_defaults: dict[str, str] = {}
    piper_locale_defaults: dict[str, str] = {}

    for family in ("piper", "kokoro", "openai", "system"):
        block = raw.get(family)
        if not isinstance(block, dict):
            raise ValueError(f"voice_catalog.json: missing '{family}' object")

        voices = block.get("voices")
        if not isinstance(voices, list):
            raise ValueError(f"voice_catalog.json: {family}.voices must be a list")

        seen_ids = kokoro_ids if family == "kokoro" else set()

        for idx, entry in enumerate(voices):
            if not isinstance(entry, dict):
                raise ValueError(f"voice_catalog.json: {family}.voices[{idx}] must be an object")

            req_fields = _PIPER_FIELDS if family == "piper" else _VOICE_FIELDS

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

            if family == "piper":
                piper_models[voice_id] = (entry["onnx"], entry["config"], entry["lang"], entry["label"])
            elif family == "kokoro":
                kokoro_items.append({"value": voice_id, "label": entry["label"], "lang": entry.get("lang") or "en"})
            else:
                voice_catalogs[family].append({"value": voice_id, "label": entry["label"]})
            if entry.get("short_name"):
                short_names[voice_id] = entry["short_name"]

        fallback_val = block.get("fallback_voice")
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
            elif family == "piper":
                piper_locale_defaults = dict(loc_defaults)

    voice_catalogs["openai"].sort(key=lambda row: row["label"].casefold())

    kokoro_block = raw["kokoro"]
    aliases = kokoro_block.get("openai_aliases")
    if not isinstance(aliases, dict):
        raise ValueError("voice_catalog.json: kokoro.openai_aliases must be an object")

    kokoro_aliases: dict[str, str] = {}
    for alias_key, target_id in aliases.items():
        if not isinstance(alias_key, str) or not alias_key or not isinstance(target_id, str) or target_id not in kokoro_ids:
            raise ValueError(
                f"voice_catalog.json: kokoro.openai_aliases.{alias_key} -> '{target_id}' is not a kokoro voice"
            )
        kokoro_aliases[alias_key] = target_id

    return {
        "piper_models": piper_models,
        "voice_catalogs": voice_catalogs,
        "kokoro_items": kokoro_items,
        "short_names": short_names,
        "kokoro_locale_defaults": kokoro_locale_defaults,
        "piper_locale_defaults": piper_locale_defaults,
        "kokoro_aliases": kokoro_aliases,
        "fallbacks": fallbacks,
    }


_parsed = _parse_catalog(_load_catalog())
PIPER_VOICE_MODELS: dict[str, PiperModel] = _parsed["piper_models"]
KOKORO_CATALOG_ITEMS: list[dict[str, str]] = _parsed["kokoro_items"]
LOCALE_TO_KOKORO_DEFAULT = _parsed["kokoro_locale_defaults"]
LOCALE_TO_PIPER_DEFAULT = _parsed["piper_locale_defaults"]
DEFAULT_VOICE_FOR_FAMILY: dict[str, str] = _parsed["fallbacks"]
PIPER_FALLBACK_VOICE = DEFAULT_VOICE_FOR_FAMILY["piper"]
KOKORO_FALLBACK_VOICE = DEFAULT_VOICE_FOR_FAMILY["kokoro"]
KOKORO_OPENAI_ALIASES = _parsed["kokoro_aliases"]
VOICE_CATALOGS = _parsed["voice_catalogs"]
_SHORT_NAMES: dict[str, str] = _parsed["short_names"]
del _parsed


def voice_short_name(voice_id: str) -> str:
    """Human name from the catalog, e.g. Thorsten for ``de_DE-thorsten-medium``."""
    # Deliberately uses first segment before ' (' rather than a trailing paren group
    clean = (voice_id or "").split(" (")[0].strip()
    return _SHORT_NAMES.get(clean) or clean or voice_id
