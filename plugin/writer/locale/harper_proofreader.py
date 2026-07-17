# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreHarper Linguistic2 XProofreader — distinct impl name from WriterAgent AI Grammar.

Registered only in the LibreHarper OXT manifest. Reuses the WriterAgent proofreader
implementation with English-only locales and a LibreHarper display name.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, cast

# Minimal stdlib-only bootstrap (same walk as ai_grammar_proofreader) before plugin imports.
_this = os.path.abspath(__file__)
for __ in range(4):
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=4, also_add_lib=True)

import unohelper


from plugin.writer.locale.ai_grammar_proofreader import (
    SERVICE_NAME,
    WriterAgentAiGrammarProofreader,
)
from plugin.writer.locale.grammar_proofread_locale import (
    bcp47_to_uno_lang_country,
    normalize_uno_locale_to_bcp47,
)

log = logging.getLogger("writeragent.grammar")

IMPLEMENTATION_NAME = "org.extension.libreharper.comp.pyuno.HarperProofreader"
SERVICE_DISPLAY_NAME = "LibreHarper"
# Must match LinguisticLibreHarperGrammar.xcu Locales (Harper English dialects).
HARPER_LOCALE_TAGS: tuple[str, ...] = ("en-US", "en-GB", "en-AU", "en-CA", "en-IN")


def normalize_harper_locale_to_bcp47(a_locale: Any) -> str | None:
    """Preserve the regional English dialects understood by Harper."""
    try:
        language = str(getattr(a_locale, "Language", "") or "").strip()
        country = str(getattr(a_locale, "Country", "") or "").strip().upper()
    except Exception:
        return None

    if "-" in language and not country:
        language, country = language.split("-", 1)
        country = country.upper()
    if language.lower() == "en":
        tag = f"en-{country}" if country else "en-US"
        if tag in HARPER_LOCALE_TAGS:
            return tag

    fallback_tag = normalize_uno_locale_to_bcp47(a_locale)
    return fallback_tag if fallback_tag in HARPER_LOCALE_TAGS else None

uno_mod: Any
try:
    uno_mod = importlib.import_module("uno")
except ImportError:
    uno_mod = None


def _harper_locale_tuple() -> tuple[Any, ...]:
    if uno_mod is None:
        return ()
    out: list[Any] = []
    try:
        for tag in HARPER_LOCALE_TAGS:
            la, ctry = bcp47_to_uno_lang_country(tag)
            out.append(cast("Any", uno_mod.createUnoStruct("com.sun.star.lang.Locale", Language=la, Country=ctry, Variant="")))
        return tuple(out)
    except Exception as e:
        log.error("[grammar] LibreHarper locale construction failed: %s", e, exc_info=True)
        return ()


class HarperProofreader(WriterAgentAiGrammarProofreader):  # pyright: ignore[reportGeneralTypeIssues]
    """Same proofreading pipeline; branded for the LibreHarper extension."""

    def __init__(self, ctx: Any, *args: Any) -> None:
        super().__init__(ctx, *args)
        self._implementation_name = IMPLEMENTATION_NAME
        self._locales = _harper_locale_tuple()

    def _normalize_locale(self, a_locale: Any) -> str | None:
        return normalize_harper_locale_to_bcp47(a_locale)

    def hasLocale(self, aLocale: Any) -> bool:
        try:
            if aLocale is None or not self._locales:
                return False
            return self._normalize_locale(aLocale) is not None
        except Exception as e:
            log.warning("[grammar] LibreHarper hasLocale: %s", e, exc_info=True)
            return False

    def getServiceDisplayName(self, aLocale: Any) -> str:
        del aLocale
        return SERVICE_DISPLAY_NAME


try:
    g_ImplementationHelper = unohelper.ImplementationHelper()
    g_ImplementationHelper.addImplementation(HarperProofreader, IMPLEMENTATION_NAME, (SERVICE_NAME,))
except (ImportError, AttributeError):
    g_ImplementationHelper = None  # type: ignore[assignment]
