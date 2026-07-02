# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv language detection via PyPI langdetect (grammar + embeddings locale)."""

from __future__ import annotations

LANGDETECT_VERSION = "1.0.9"


def _import_error() -> ImportError:
    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    return ImportError(
        f"langdetect is not installed in the configured Python venv. "
        f"Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
    )


def _raw_lang_to_bcp47(raw: str | None) -> str | None:
    if not raw:
        return None
    from plugin.writer.locale.grammar_proofread_locale import normalize_detected_bcp47

    text = str(raw).strip()
    if text == "zh-cn":
        text = "zh-CN"
    elif text == "zh-tw":
        text = "zh-TW"
    else:
        parts = text.split("-")
        if len(parts) == 2:
            text = f"{parts[0]}-{parts[1].upper()}"
    return normalize_detected_bcp47(text) or text


def _detect_raw_lang(text: str) -> str | None:
    sample = str(text or "").strip()
    if not sample:
        return None
    try:
        from langdetect import detect_langs
        from langdetect.lang_detect_exception import LangDetectException
    except ImportError as exc:
        raise _import_error() from exc
    try:
        hits = detect_langs(sample)
    except LangDetectException:
        return None
    if not hits:
        return None
    return str(hits[0].lang)


def detect_lang_sample(text: str) -> str | None:
    """Detect one text sample; return BCP-47 tag or None."""
    return _raw_lang_to_bcp47(_detect_raw_lang(text))


def detect_lang_batch(texts: list[str]) -> list[str | None]:
    """Detect language for each text; return aligned BCP-47 tags (None when unknown)."""
    if texts is None:
        return []
    return [detect_lang_sample(text) for text in texts]


__all__ = ["LANGDETECT_VERSION", "detect_lang_batch", "detect_lang_sample"]
