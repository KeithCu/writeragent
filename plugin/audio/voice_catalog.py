# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the TTS voice catalog once from ``data/voice_catalog.json``.

Piper/Kokoro/OpenAI/OS voice lists, locale defaults, and the OpenAI→Kokoro
alias map used to be inline dicts in ``tts_service``. Settings and the
sidebar both read this asset so those lists cannot drift apart. JSON (not
YAML) because the LibreOffice runtime does not ship PyYAML.
"""

from __future__ import annotations

import json
import os
from typing import Any

# onnx relative path, config relative path, lang code, display label.
PiperModel = tuple[str, str, str, str]

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "voice_catalog.json")


def _load_catalog(path: str = CATALOG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("voice catalog asset must be a JSON object")
    return data


def _family(raw: dict[str, Any], name: str) -> dict[str, Any]:
    block = raw.get(name)
    if not isinstance(block, dict):
        raise ValueError("voice catalog missing %s" % name)
    return block


def _voice_rows(block: dict[str, Any], name: str) -> list[dict[str, str]]:
    rows = block.get("voices")
    if not isinstance(rows, list):
        raise ValueError("voice catalog %s.voices must be a list" % name)
    voices: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("voice catalog %s.voices entries must be objects" % name)
        voices.append({str(key): str(val) for key, val in item.items() if val is not None})
    return voices


def _locale_defaults(block: dict[str, Any]) -> dict[str, str]:
    raw = block.get("locale_defaults") or {}
    if not isinstance(raw, dict):
        raise ValueError("voice catalog locale_defaults must be an object")
    return {str(key): str(val) for key, val in raw.items()}


def _fallback_voice(block: dict[str, Any], default: str) -> str:
    val = block.get("fallback_voice")
    return str(val) if val else default


def _option_list(voices: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"value": item["id"], "label": item["label"]} for item in voices]


_RAW = _load_catalog()
_PIPER = _family(_RAW, "piper")
_KOKORO = _family(_RAW, "kokoro")
_OPENAI = _family(_RAW, "openai")
_SYSTEM = _family(_RAW, "system")

_PIPER_ROWS = _voice_rows(_PIPER, "piper")
_KOKORO_ROWS = _voice_rows(_KOKORO, "kokoro")

# Same tuple shape the service used when the map lived inline.
PIPER_VOICE_MODELS: dict[str, PiperModel] = {
    item["id"]: (item["onnx"], item["config"], item["lang"], item["label"])
    for item in _PIPER_ROWS
}

KOKORO_CATALOG_ITEMS: list[dict[str, str]] = [
    {"value": item["id"], "label": item["label"], "lang": item.get("lang", "en")}
    for item in _KOKORO_ROWS
]

LOCALE_TO_PIPER_DEFAULT: dict[str, str] = _locale_defaults(_PIPER)
LOCALE_TO_KOKORO_DEFAULT: dict[str, str] = _locale_defaults(_KOKORO)

PIPER_FALLBACK_VOICE = _fallback_voice(_PIPER, "en_US-lessac-medium")
KOKORO_FALLBACK_VOICE = _fallback_voice(_KOKORO, "af_bella")

_aliases = _KOKORO.get("openai_aliases") or {}
if not isinstance(_aliases, dict):
    raise ValueError("voice catalog kokoro.openai_aliases must be an object")
KOKORO_OPENAI_ALIASES: dict[str, str] = {str(key): str(val) for key, val in _aliases.items()}

VOICE_CATALOGS: dict[str, list[dict[str, str]]] = {
    "kokoro": [{"value": item["value"], "label": item["label"]} for item in KOKORO_CATALOG_ITEMS],
    "piper": [{"value": voice_id, "label": meta[3]} for voice_id, meta in PIPER_VOICE_MODELS.items()],
    "openai": _option_list(_voice_rows(_OPENAI, "openai")),
    "system": _option_list(_voice_rows(_SYSTEM, "system")),
}

DEFAULT_VOICE_FOR_FAMILY: dict[str, str] = {
    "kokoro": KOKORO_FALLBACK_VOICE,
    "piper": PIPER_FALLBACK_VOICE,
    "openai": _fallback_voice(_OPENAI, "alloy"),
    "system": _fallback_voice(_SYSTEM, "default"),
}

_SHORT_NAMES: dict[str, str] = {}
for _row in _PIPER_ROWS + _KOKORO_ROWS:
    short = _row.get("short_name", "")
    if short:
        _SHORT_NAMES[_row["id"]] = short


def voice_short_name(voice_id: str) -> str:
    """Human name from the catalog, e.g. Thorsten for ``de_DE-thorsten-medium``."""
    clean = (voice_id or "").split(" (")[0].strip()
    named = _SHORT_NAMES.get(clean)
    if named:
        return named
    return clean or voice_id
