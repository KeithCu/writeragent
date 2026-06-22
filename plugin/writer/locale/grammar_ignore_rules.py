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


def is_rule_ignored(rule_identifier: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Return True when ``rule_identifier`` matches document or global ignore lists."""
    if rule_identifier.startswith(WA_G_RULE_PREFIX):
        norm_reason = normalize_reason(rule_identifier[len(WA_G_RULE_PREFIX) :])
        return norm_reason in doc_ignored or rule_identifier in global_ignored
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
