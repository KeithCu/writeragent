# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal no-op Linguistic2 proofreader for isolating native registration crashes."""

from __future__ import annotations

from typing import Any, no_type_check

import uno
import unohelper

from com.sun.star.lang import Locale, XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales

from plugin.modules.writer.grammar_locale_registry import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_uno_lang_country,
    normalize_uno_locale_to_bcp47,
)

IMPLEMENTATION_NAME = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"
SERVICE_NAME = "com.sun.star.linguistic2.Proofreader"


@no_type_check
class WriterAgentAiGrammarProofreaderStub(
    unohelper.Base,
    XProofreader,
    XServiceInfo,
    XServiceName,
    XServiceDisplayName,
    XSupportedLocales,
):
    """No-op proofreader with the same implementation id as the real component."""

    def __init__(self, ctx: Any, *args: Any):
        # LibreOffice passes compatibility args when enumerating Linguistic services.
        del args
        self.ctx = ctx
        _loc: list[Locale] = []
        for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
            la, c = bcp47_to_uno_lang_country(tag)
            _loc.append(Locale(la, c, ""))
        self._locales = tuple(_loc)

    def getServiceName(self) -> str:
        return IMPLEMENTATION_NAME

    def getImplementationName(self) -> str:
        return IMPLEMENTATION_NAME

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName == SERVICE_NAME

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return (SERVICE_NAME,)

    def hasLocale(self, aLocale: Any) -> bool:
        if aLocale is None or not self._locales:
            return False
        return normalize_uno_locale_to_bcp47(aLocale) is not None

    def getLocales(self) -> tuple[Any, ...]:
        return self._locales

    def isSpellChecker(self) -> bool:
        return False

    def doProofreading(
        self,
        aDocumentIdentifier: str,
        aText: str,
        aLocale: Any,
        nStartOfSentencePosition: int,
        nSuggestedBehindEndOfSentencePosition: int,
        aProperties: Any,
    ) -> Any:
        del aProperties
        res: Any = uno.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult")
        setattr(res, "aDocumentIdentifier", aDocumentIdentifier)
        setattr(res, "aText", aText)
        setattr(res, "aLocale", aLocale)
        setattr(res, "nStartOfSentencePosition", nStartOfSentencePosition)
        setattr(res, "nStartOfNextSentencePosition", nSuggestedBehindEndOfSentencePosition)
        setattr(res, "nBehindEndOfSentencePosition", nSuggestedBehindEndOfSentencePosition)
        setattr(res, "aProperties", ())
        setattr(res, "xProofreader", self)
        setattr(res, "aErrors", ())
        return res

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        del aRuleIdentifier, aLocale

    def resetIgnoreRules(self) -> None:
        return None

    def getServiceDisplayName(self, aLocale: Any) -> str:
        del aLocale
        return "WriterAgent AI Grammar Stub"


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    WriterAgentAiGrammarProofreaderStub,
    IMPLEMENTATION_NAME,
    (SERVICE_NAME,),
)
