# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ignore-rule matching for grammar proofreading (doc + global)."""

from __future__ import annotations

from typing import Any

from .grammar_proofread_cache import ignored_rules_snapshot
from .grammar_proofread_locale import normalize_reason

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


def is_prefixed_rule_ignored(rule_identifier: str, prefix: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Match ignore lists for stable prefixed rule ids (bare code in doc; full id in session global)."""
    code = parse_prefixed_rule_identifier(rule_identifier, prefix)
    if code is None:
        return False
    return rule_identifier in global_ignored or code in doc_ignored


def is_rule_ignored(rule_identifier: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Return True when ``rule_identifier`` matches document or global ignore lists."""
    if rule_identifier.startswith(WA_G_RULE_PREFIX):
        norm_reason = normalize_reason(rule_identifier[len(WA_G_RULE_PREFIX) :])
        return norm_reason in doc_ignored or rule_identifier in global_ignored
    for prefix in STABLE_RULE_PREFIXES:
        if is_prefixed_rule_ignored(rule_identifier, prefix, doc_ignored, global_ignored):
            return True
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
            continue
        bare_code = None
        for prefix in STABLE_RULE_PREFIXES:
            if rule_id.startswith(prefix):
                bare_code = parse_prefixed_rule_identifier(rule_id, prefix)
                break
        if bare_code:
            ignored_reasons.add(bare_code)
        else:
            ignored_reasons.add(normalize_reason(rule_id))
    return ignored_reasons
