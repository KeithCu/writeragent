# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ignore-rule matching for grammar proofreading (doc + global)."""

from __future__ import annotations

import re
from typing import Any

from .grammar_proofread_cache import ignored_rules_snapshot

# The venv LanguageTool checker imports this leaf, not this module, because
# this module imports grammar_proofread_cache, which imports config.
from plugin.framework.grammar_rule_ids import (
    HARPER_RULE_PREFIX,
    LANGUAGETOOL_RULE_PREFIX,
    STABLE_RULE_PREFIXES,
    WA_G_RULE_PREFIX,
    make_rule_identifier,
    parse_prefixed_rule_identifier,
)

__all__ = [
    "HARPER_RULE_PREFIX",
    "LANGUAGETOOL_RULE_PREFIX",
    "STABLE_RULE_PREFIXES",
    "WA_G_RULE_PREFIX",
    "canonical_rule_key",
    "collect_ignored_reasons",
    "doc_ignored_rules",
    "is_rule_ignored",
    "make_rule_identifier",
    "normalize_reason",
    "parse_prefixed_rule_identifier",
]

GRAMMAR_NORMALIZE_REASON_RE = re.compile(r"[^\w\s]")


def normalize_reason(reason: str) -> str:
    """Canonical/generalized representation of an error reason to group similar rules.

    - Converts to lowercase.
    - Strips punctuation and non-alphanumeric characters (keeping all word contents).
    - Collapses spaces into a single-space separated canonical string.
    """
    if not reason:
        return ""
    s = reason.lower().strip()
    # Strip non-alphanumeric characters (keeping word contents)
    s = GRAMMAR_NORMALIZE_REASON_RE.sub("", s).replace("_", "")
    return " ".join(s.split())


def canonical_rule_key(rule_identifier: str) -> str | None:
    """Document-set key for ``rule_identifier`` (prompt filter + persistence); None when empty."""
    if rule_identifier.startswith(WA_G_RULE_PREFIX):
        return normalize_reason(rule_identifier[len(WA_G_RULE_PREFIX) :]) or None
    for prefix in STABLE_RULE_PREFIXES:
        code = parse_prefixed_rule_identifier(rule_identifier, prefix)
        if code is not None:
            return code
    return normalize_reason(rule_identifier) or None


def is_rule_ignored(rule_identifier: str, doc_ignored: set[str], global_ignored: set[str]) -> bool:
    """Return True when ``rule_identifier`` matches document or global ignore lists."""
    if not rule_identifier:
        return False
    if rule_identifier in global_ignored or rule_identifier in doc_ignored:
        return True
    key = canonical_rule_key(rule_identifier)
    return key is not None and (key in doc_ignored or key in global_ignored)


def doc_ignored_rules(ctx: Any, doc_id: str) -> set[str]:
    """Document-scoped ignored rule reasons (normalized strings in persistence)."""
    from .grammar_persistence import get_persistence

    p = get_persistence(ctx, doc_id)
    return p.ignored_rules_snapshot() if p else set()


def collect_ignored_reasons(ctx: Any, doc_id: str) -> set[str]:
    """Document + global ignored grammar rules, normalized for prompt-side filtering.

    Not the same as ``is_rule_ignored`` (membership test vs set of canonical keys).
    """
    ignored_reasons = doc_ignored_rules(ctx, doc_id)
    for rule_id in ignored_rules_snapshot():
        key = canonical_rule_key(rule_id)
        if key is not None:
            ignored_reasons.add(key)
    return ignored_reasons
