# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure rule-id strings shared by the host ignore list and the venv LanguageTool checker. This module must not import plugin.writer."""

from __future__ import annotations

WA_G_RULE_PREFIX = "wa_g_rule||"
HARPER_RULE_PREFIX = "harper||"
LANGUAGETOOL_RULE_PREFIX = "languagetool||"
STABLE_RULE_PREFIXES = (HARPER_RULE_PREFIX, LANGUAGETOOL_RULE_PREFIX)


def make_rule_identifier(prefix: str, rule_code: str) -> str:
    """Build a prefixed grammar rule id for UNO ``aRuleIdentifier``."""
    return f"{prefix}{rule_code}"


def parse_prefixed_rule_identifier(rule_identifier: str, prefix: str) -> str | None:
    """Return rule code suffix for a prefixed id, or ``None`` when prefix does not match."""
    if not rule_identifier.startswith(prefix):
        return None
    code = rule_identifier[len(prefix) :].strip()
    return code or None


def bare_code_for_persistence(rule_identifier: str, prefix: str) -> str:
    """Bare rule code stored in document ``ignored_rules`` (fallback: full id)."""
    return parse_prefixed_rule_identifier(rule_identifier, prefix) or rule_identifier
