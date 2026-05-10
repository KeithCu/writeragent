# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python helpers for AI grammar proofreading (facade: text pipeline, cache, work items)."""

from __future__ import annotations

from .grammar_locale_registry import GRAMMAR_REGISTRY_LOCALE_TAGS as _GRAMMAR_REGISTRY_LOCALE_TAGS
from .grammar_proofread_cache import (
    MAX_CACHE_SIZE,
    MAX_RECENT_INCOMPLETE_SCAN,
    _normalize_for_sentence_cache,
    cache_clear,
    cache_get_sentence,
    cache_put_sentence,
    clear_sentence_cache,
    fingerprint_for_text,
    ignore_rule_add,
    ignore_rules_clear,
    ignored_rules_snapshot,
    make_sentence_key,
    sentence_cache_key_prefix,
    sentence_identity_fp,
    should_evict_incomplete_prefix_predecessor,
)
from .grammar_proofread_text import (
    NormalizedProofError,
    anchor_wrong_in_window,
    extend_through_trailing_whitespace,
    normalize_errors_for_text,
    parse_grammar_json,
    split_into_sentences,
)
from .grammar_proofread_work_item import GrammarWorkItem, deduplicate_grammar_batch

# Re-export: hyphenated tags for ``LinguisticWriterAgentGrammar.xcu`` and tests (see
# ``grammar_locale_registry`` — same list as repo-root ``locales/`` + ``en`` variants).
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = _GRAMMAR_REGISTRY_LOCALE_TAGS
