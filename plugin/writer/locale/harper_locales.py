# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harper grammar-checker supported locales and dialect mappings.

Locales must match the XCU Locales order for LinguisticLibreHarperGrammar.
Unknown tags default to American via .get(..., "American") in _harper_lsp_settings.
"""

from __future__ import annotations

# XCU Locales order: en-US en-GB en-AU en-CA en-IN
HARPER_DIALECTS: dict[str, str] = {
    "en-US": "American",
    "en-GB": "British",
    "en-AU": "Australian",
    "en-CA": "Canadian",
    "en-IN": "Indian",
}

HARPER_LOCALE_TAGS: tuple[str, ...] = tuple(HARPER_DIALECTS)
