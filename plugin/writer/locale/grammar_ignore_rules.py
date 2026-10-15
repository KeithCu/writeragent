# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ignore-rule matching for grammar proofreading (doc + global)."""

from __future__ import annotations

from typing import Any

from .grammar_proofread_cache import ignored_rules_snapshot
from .grammar_proofread_locale import normalize_reason

# The venv LanguageTool checker imports this leaf, not this module, because
# this module imports grammar_proofread_cache, which imports config.
from plugin.framework.grammar_rule_ids import (
    HARPER_RULE_PREFIX,
    LANGUAGETOOL_RULE_PREFIX,
    STABLE_RULE_PREFIXES,
    WA_G_RULE_PREFIX,
    bare_code_for_persistence,
    make_rule_identifier,
    parse_prefixed_rule_identifier,
)

__all__ = [
    "HARPER_RULE_PREFIX",
    "LANGUAGETOOL_RULE_PREFIX",
    "STABLE_RULE_PREFIXES",
    "WA_G_RULE_PREFIX",
    "bare_code_for_persistence",
    "canonical_rule_keys",
    "collect_ignored_reasons",
    "doc_ignored_rules",
    "is_prefixed_rule_ignored",
    "is_rule_ignored",
    "make_rule_identifier",
    "parse_prefixed_rule_identifier",
]


def canonical_rule_keys(rule_identifier: str) -> tuple[str, ...]:
    """Document-set keys for ``rule_identifier`` (prompt filter + persistence).

    Session-global ignore still stores the full prefixed id. Membership tests
    both this tuple against the doc set and the raw id against the global set.
    """
    if rule_identifier.startswith(WA_G_RULE_PREFIX):
        return (normalize_reason(rule_identifier[len(WA_G_RULE_PREFIX) :]),)
    for prefix in STABLE_RULE_PREFIXES:
        code = parse_prefixed_rule_identifier(rule_identifier, prefix)
        if code is not None:
            return (code,)
    return (normalize_reason(rule_identifier),)


def is_prefixed_rule_ignored(rule_identifier: str, prefix: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Match ignore lists for stable prefixed rule ids (bare code in doc; full id in session global)."""
    code = parse_prefixed_rule_identifier(rule_identifier, prefix)
    if code is None:
        return False
    return rule_identifier in global_ignored or code in doc_ignored


def is_rule_ignored(rule_identifier: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Return True when ``rule_identifier`` matches document or global ignore lists."""
    if rule_identifier in global_ignored:
        return True
    if rule_identifier in doc_ignored:
        return True
    return any(key in doc_ignored or key in global_ignored for key in canonical_rule_keys(rule_identifier))


def doc_ignored_rules(ctx: Any, doc_id: str) -> set[str]:
    """Document-scoped ignored rule reasons (normalized strings in persistence)."""
    from .grammar_persistence import get_persistence

    p = get_persistence(ctx, doc_id)
    return set(p._ignored_rules) if p else set()


def collect_ignored_reasons(ctx: Any, doc_id: str) -> set[str]:
    """Document + global ignored grammar rules, normalized for prompt-side filtering.

    Not the same as ``is_rule_ignored`` (membership test vs set of canonical keys).
    """
    ignored_reasons = doc_ignored_rules(ctx, doc_id)
    for rule_id in ignored_rules_snapshot():
        ignored_reasons.update(canonical_rule_keys(rule_id))
    return ignored_reasons
