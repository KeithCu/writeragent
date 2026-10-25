# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreHarper Linguistic2 XProofreader — distinct impl name from WriterAgent AI Grammar.

Registered only in the LibreHarper OXT manifest. Reuses the WriterAgent proofreader
implementation with English-only locales and a LibreHarper display name.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

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
from plugin.framework.grammar_rule_ids import normalize_detected_bcp47
from plugin.writer.locale.grammar_proofread_locale import uno_locales_for_tags

log = logging.getLogger("writeragent.grammar")

from plugin.framework.constants import EXTENSION_ID_LIBREHARPER
from plugin.writer.locale.harper import HARPER_LOCALE_TAGS

IMPLEMENTATION_NAME = f"{EXTENSION_ID_LIBREHARPER}.comp.pyuno.HarperProofreader"
SERVICE_DISPLAY_NAME = "LibreHarper"
def normalize_harper_locale_to_bcp47(a_locale: Any) -> str | None:
    """Preserve the regional English dialects understood by Harper."""
    if a_locale is None:
        return None
    language = str(getattr(a_locale, "Language", "") or "").strip()
    country = str(getattr(a_locale, "Country", "") or "").strip()
    tag = language
    # Explicit Country wins over a plain Language; a Language that already
    # carries a region ("en-GB", "en_GB") keeps it; normalize splits and
    # upper-cases either form.
    if country and language and "-" not in language and "_" not in language:
        tag = f"{language}-{country}"
    normalized = normalize_detected_bcp47(tag)
    return normalized if normalized in HARPER_LOCALE_TAGS else None

def _harper_locale_tuple() -> tuple[Any, ...]:
    return uno_locales_for_tags(HARPER_LOCALE_TAGS)


class HarperProofreader(WriterAgentAiGrammarProofreader):  # pyright: ignore[reportGeneralTypeIssues]
    """Same proofreading pipeline; branded for the LibreHarper extension."""

    _always_enabled: bool = True
    _checker_identity: str
    _provider: str
    _implementation_name: str
    _locales: tuple[Any, ...]
    _locales_degraded: bool
    _locale_retry_logged: bool

    def __init__(self, ctx: Any, *args: Any) -> None:
        # Pin package id before base init and update check: WriterAgent may also be installed, and
        # resolve_package_extension_id prefers the first known id with a location.
        # Identity before base init: register_live_proofreader / warmup may run doProofreading.
        self._checker_identity = "harper"
        self._provider = "harper"
        # Set before super(): registration can observe the instance, and a
        # failed locale build must not look like "no English locales".
        self._locales_degraded = False
        self._locale_retry_logged = False
        # uno.bin register/enable has no VCL. Warmup status and the weekly
        # update dialog both touch Desktop; skip those side effects here (#768).
        skip_register_ui = False
        try:
            from plugin.framework.uno_context import desktop_create_is_unsafe, set_package_extension_id

            set_package_extension_id(EXTENSION_ID_LIBREHARPER)
            skip_register_ui = desktop_create_is_unsafe()
            if not skip_register_ui:
                from plugin.chatbot.extension_update_check import schedule_extension_update_check_once

                schedule_extension_update_check_once(ctx, EXTENSION_ID_LIBREHARPER)
        except Exception as e:
            log.warning("[grammar] LibreHarper extension update check schedule failed: %s", e)

        super().__init__(ctx, *args)
        self._implementation_name = IMPLEMENTATION_NAME
        try:
            self._locales = _harper_locale_tuple()
        except Exception as exc:
            # Registered degraded: hasLocale / getLocales retry instead of
            # caching ``()`` and silently skipping every proofread.
            self._locales = ()
            self._locales_degraded = True
            log.error(
                "[grammar] LibreHarper proofreader registered degraded; locale list unavailable: %s",
                exc,
                exc_info=True,
            )
        if skip_register_ui:
            return
        # LibreHarper has no OnStartApp job; start harper-ls only once the profile path exists.
        try:
            from plugin.framework.config import init_config, user_config_dir
            from plugin.writer.locale.harper import maybe_start_harper_async

            init_config(ctx)
            ucd = user_config_dir() or ""
            if ucd:
                maybe_start_harper_async(ctx, user_config_dir=ucd)
        except Exception as e:
            log.warning("[grammar] LibreHarper harper warmup start failed: %s", e)

    def _normalize_locale(self, a_locale: Any) -> str | None:
        return normalize_harper_locale_to_bcp47(a_locale)

    def _ensure_harper_locales(self) -> None:
        """Retry locale construction after a degraded register.

        ``uno`` missing (unit tests) returns ``()`` without raising and is not
        degraded. A failed ``createUnoStruct`` is degraded and retried so a
        transient UNO error does not disable Harper for the session.
        """
        if self._locales or not self._locales_degraded:
            return
        try:
            built = _harper_locale_tuple()
        except Exception as exc:
            if not self._locale_retry_logged:
                self._locale_retry_logged = True
                log.warning("[grammar] LibreHarper locale retry failed: %s", exc, exc_info=True)
            return
        if not built:
            return
        self._locales = built
        self._locales_degraded = False
        self._locale_retry_logged = False
        log.info("[grammar] LibreHarper locale list recovered (%s)", len(built))

    def hasLocale(self, aLocale: Any) -> bool:
        try:
            if aLocale is None:
                return False
            self._ensure_harper_locales()
            if not self._locales:
                return False
            return self._normalize_locale(aLocale) is not None
        except Exception as e:
            log.warning("[grammar] LibreHarper hasLocale: %s", e, exc_info=True)
            return False

    def getLocales(self) -> tuple[Any, ...]:
        try:
            self._ensure_harper_locales()
            return self._locales
        except Exception as e:
            log.warning("[grammar] LibreHarper getLocales: %s", e, exc_info=True)
            return ()

    def getServiceDisplayName(self, aLocale: Any) -> str:
        del aLocale
        return SERVICE_DISPLAY_NAME


try:
    g_ImplementationHelper = unohelper.ImplementationHelper()
    g_ImplementationHelper.addImplementation(HarperProofreader, IMPLEMENTATION_NAME, (SERVICE_NAME,))
except (ImportError, AttributeError):
    g_ImplementationHelper = None  # type: ignore[assignment]
