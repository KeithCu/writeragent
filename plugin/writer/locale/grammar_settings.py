# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Leaf module for grammar engine provider and identity settings.

Imports only framework config and uno_context; must not import sibling
modules under plugin.writer.locale or plugin.framework.client at module level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from plugin.framework import config, uno_context

_LOCAL = frozenset({"harper", "languagetool", "vale"})

GrammarProvider = Literal["off", "llm", "languagetool", "vale", "harper"]
GrammarDetectLanguageMode = Literal["off", "llm", "langdetect"]


@dataclass(frozen=True)
class GrammarProfile:
    provider: str
    identity: str
    always_enabled: bool


def provider() -> str:
    """Return the active grammar provider name ('off', 'llm', 'languagetool', 'vale', or 'harper')."""
    if uno_context.is_libreharper():
        return "harper"

    val = config.get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return "llm" if val else "off"
    val_str = str(val).strip().lower()
    if val_str == "true":
        return "llm"
    if val_str in ("llm", "languagetool", "vale", "harper"):
        return val_str

    return "off"


def is_grammar_enabled() -> bool:
    """True if the grammar checker is enabled on the Doc tab (LLM, LanguageTool, Vale, or Harper)."""
    return provider() != "off"


def is_local(name: str) -> bool:
    """Return True if the grammar provider runs locally without external LLM calls."""
    return name in _LOCAL


def detect_language_mode() -> GrammarDetectLanguageMode:
    """Read ``doc.grammar_proofreader_detect_language`` (off / llm / langdetect; legacy bool → llm/off)."""
    raw = config.get_config("doc.grammar_proofreader_detect_language")
    if isinstance(raw, bool):
        return "llm" if raw else "off"
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in ("off", "llm", "langdetect"):
            return v  # type: ignore[return-value]
        if v in ("1", "true", "yes", "on"):
            return "llm"
    return "off"


def checker_identity(name: str | None = None) -> str:
    """Stable cache/file identity for the grammar checker.

    Local engines use their name ('harper' / 'languagetool' / 'vale').
    'off' returns 'off'.
    LLM (or unspecified/custom) uses 'llm:' plus the resolved model from get_grammar_model().
    """
    active = provider() if name is None else name
    if is_local(active):
        return active
    if active == "off":
        return "off"
    from plugin.framework.client.model_fetcher import get_grammar_model

    return f"llm:{get_grammar_model() or ''}"
