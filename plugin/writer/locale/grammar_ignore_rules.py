# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ignore-rule matching for grammar proofreading (doc + global)."""

from __future__ import annotations

import re
from typing import Any

from .grammar_proofread_cache import ignored_rules_snapshot
from .grammar_proofread_locale import normalize_reason

WA_G_RULE_PREFIX = "wa_g_rule||"
HARPER_RULE_PREFIX = "harper||"
LANGUAGETOOL_RULE_PREFIX = "languagetool||"

_BARE_LANGUAGETOOL_RULE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def parse_prefixed_rule_identifier(rule_identifier: str, prefix: str) -> str | None:
    """Return rule code suffix for a prefixed id, or ``None`` when prefix does not match."""
    if not rule_identifier.startswith(prefix):
        return None
    code = rule_identifier[len(prefix) :].strip()
    return code or None


def parse_harper_rule_identifier(rule_identifier: str) -> str | None:
    """Return Harper rule code suffix (e.g. ``SpellCheck``), or ``None`` when not a Harper id."""
    return parse_prefixed_rule_identifier(rule_identifier, HARPER_RULE_PREFIX)


def parse_languagetool_rule_identifier(rule_identifier: str) -> str | None:
    """Return LanguageTool rule id suffix, or ``None`` when not a LanguageTool id."""
    return parse_prefixed_rule_identifier(rule_identifier, LANGUAGETOOL_RULE_PREFIX)


def harper_rule_code(rule_identifier: str) -> str | None:
    """Alias for :func:`parse_harper_rule_identifier`."""
    return parse_harper_rule_identifier(rule_identifier)


def is_bare_languagetool_rule_id(rule_identifier: str) -> bool:
    """True for legacy bare LT ids (uppercase snake_case, e.g. ``ENGLISH_WORD_REPEAT_RULE``)."""
    if not rule_identifier or rule_identifier.startswith((WA_G_RULE_PREFIX, HARPER_RULE_PREFIX, LANGUAGETOOL_RULE_PREFIX)):
        return False
    return _BARE_LANGUAGETOOL_RULE_ID_RE.fullmatch(rule_identifier) is not None


def is_prefixed_rule_ignored(rule_identifier: str, prefix: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Match ignore lists for stable prefixed rule ids (full id or bare code suffix)."""
    code = parse_prefixed_rule_identifier(rule_identifier, prefix)
    if code is None:
        return False
    return (
        rule_identifier in doc_ignored
        or rule_identifier in global_ignored
        or code in doc_ignored
        or code in global_ignored
    )


def is_rule_ignored(rule_identifier: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Return True when ``rule_identifier`` matches document or global ignore lists."""
    if rule_identifier.startswith(WA_G_RULE_PREFIX):
        norm_reason = normalize_reason(rule_identifier[len(WA_G_RULE_PREFIX) :])
        return norm_reason in doc_ignored or rule_identifier in global_ignored
    if is_prefixed_rule_ignored(rule_identifier, HARPER_RULE_PREFIX, doc_ignored, global_ignored):
        return True
    if is_prefixed_rule_ignored(rule_identifier, LANGUAGETOOL_RULE_PREFIX, doc_ignored, global_ignored):
        return True
    if is_bare_languagetool_rule_id(rule_identifier):
        prefixed = f"{LANGUAGETOOL_RULE_PREFIX}{rule_identifier}"
        return (
            rule_identifier in doc_ignored
            or rule_identifier in global_ignored
            or prefixed in doc_ignored
            or prefixed in global_ignored
        )
    return rule_identifier in doc_ignored or rule_identifier in global_ignored


def doc_ignored_rules(ctx: Any, doc_id: str) -> set[str]:
    """Document-scoped ignored rule reasons (normalized strings in persistence)."""
    from .grammar_persistence import get_persistence

    p = get_persistence(ctx, doc_id)
    return set(p._ignored_rules) if p else set()


def collect_ignored_reasons(ctx: Any, doc_id: str) -> set[str]:
    """Document + global ignored grammar rules, normalized for prompt-side filtering."""
    ignored_reasons = doc_ignored_rules(ctx, doc_id)
    for rule_id in ignored_rules_snapshot():
        if rule_id.startswith(WA_G_RULE_PREFIX):
            ignored_reasons.add(normalize_reason(rule_id[len(WA_G_RULE_PREFIX) :]))
        else:
            ignored_reasons.add(normalize_reason(rule_id))
    return ignored_reasons
