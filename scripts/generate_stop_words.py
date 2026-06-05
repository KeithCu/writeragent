#!/usr/bin/env python3
"""Regenerate plugin/writer/locale/stop_words.py from stopwords-iso (MIT).

Usage (repo root):
  python scripts/generate_stop_words.py
"""
from __future__ import annotations

import json
import os
import urllib.request

SNOWBALL_TO_ISO = {
    "arabic": "ar", "armenian": "hy", "basque": "eu", "catalan": "ca",
    "danish": "da", "dutch": "nl", "english": "en", "esperanto": "eo",
    "estonian": "et", "finnish": "fi", "french": "fr", "german": "de",
    "greek": "el", "hindi": "hi", "hungarian": "hu", "indonesian": "id",
    "irish": "ga", "italian": "it", "lithuanian": "lt", "norwegian": "no",
    "portuguese": "pt", "romanian": "ro", "russian": "ru", "serbian": "hr",
    "spanish": "es", "swedish": "sv", "tamil": "ta", "turkish": "tr", "yiddish": "yi",
}

# Not in stopwords-iso.
MANUAL: dict[str, list[str]] = {
    "nepali": ["अनि", "इ", "उ", "एक", "कि", "को", "छ", "छन्", "तथा", "तिनी", "त्यो", "देखि", "न", "मा", "मेरो", "यो", "र", "लाई", "हामी", "हो", "हुन्"],
    "tamil": ["அது", "அவர்", "அவள்", "அவர்கள்", "இது", "உம்", "ஒரு", "இந்த", "அந்த", "என்", "நான்", "நாம்", "மற்றும்", "மீது", "இல்", "க்கு", "ஆல்", "ஆக", "என்று", "எனினும்", "போல", "உடன்", "அல்லது", "ஆனால்"],
    "yiddish": ["און", "די", "אין", "פון", "צו", "איז", "עס", "זי", "ער", "מיר", "נישט", "אויף", "מיט", "דער", "דאס", "אָבער", "נאָר", "אַ", "אַז", "וואָס", "ביי", "זיך", "אים", "זיי", "אונז"],
}

STOPWORDS_ISO_URL = "https://raw.githubusercontent.com/stopwords-iso/stopwords-iso/master/stopwords-iso.json"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _from_iso(words: list[str]) -> list[str]:
    out: list[str] = []
    for w in words:
        if not isinstance(w, str):
            continue
        w = w.lower().strip()
        if len(w) < 2 or len(w) > 24:
            continue
        if any(ch.isdigit() for ch in w):
            continue
        out.append(w)
    return sorted(set(out))


def main() -> None:
    from plugin.writer.locale.linguistic_index import _ISO_TO_SNOWBALL

    with urllib.request.urlopen(STOPWORDS_ISO_URL, timeout=30) as r:
        all_sw = json.load(r)

    langs = sorted(set(_ISO_TO_SNOWBALL.values()))
    data: dict[str, list[str]] = {}
    for lang in langs:
        if lang in MANUAL:
            data[lang] = MANUAL[lang]
        else:
            iso = SNOWBALL_TO_ISO[lang]
            data[lang] = _from_iso(all_sw[iso])

    lines = [
        "# WriterAgent - AI Writing Assistant for LibreOffice",
        "# Copyright (c) 2024 John Balis",
        "# Copyright (c) 2026 KeithCu (modifications and relicensing)",
        "#",
        "# Grammar stop words keyed by Snowball algorithm name (linguistic index + web-research cache).",
        "# Generated from stopwords-iso (MIT): https://github.com/stopwords-iso/stopwords-iso",
        "# Regenerate: python scripts/generate_stop_words.py",
        "# Serbian -> Croatian (hr). Nepali, Tamil, Yiddish: hand-curated (not in stopwords-iso).",
        "",
        "from __future__ import annotations",
        "",
        "STOP_WORDS: dict[str, frozenset[str]] = {",
    ]
    for lang in langs:
        chunk = ", ".join(repr(w) for w in data[lang])
        lines.append(f'    "{lang}": frozenset({{{chunk}}}),')
    lines.extend([
        "}",
        "",
        "STOP_WORDS_FALLBACK: frozenset[str] = frozenset()",
        "",
        "",
        "def stop_words_for_snowball(snowball_lang: str) -> frozenset[str]:",
        '    """Return grammar stop words for a Snowball algorithm name, or empty."""',
        "    return STOP_WORDS.get(snowball_lang, STOP_WORDS_FALLBACK)",
        "",
    ])

    out_path = os.path.join(_repo_root(), "plugin", "writer", "locale", "stop_words.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {out_path} ({len(langs)} languages)")


if __name__ == "__main__":
    main()
