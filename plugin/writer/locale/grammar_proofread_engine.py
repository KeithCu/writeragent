# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal compatibility export — prefer direct imports from sibling modules.

Use ``grammar_proofread_text`` (BreakIterator split, offsets, sentence scheduling),
``grammar_proofread_locale`` (Unicode terminals, abbrev/Thai chunking, worker caps/prompt, JSON parse),
``grammar_proofread_cache`` (LRU), ``grammar_proofread_work_item``,
``grammar_locale_registry`` (``GRAMMAR_REGISTRY_LOCALE_TAGS``), and ``grammar_work_queue``.
"""

from __future__ import annotations

from .grammar_locale_registry import GRAMMAR_REGISTRY_LOCALE_TAGS

__all__ = ["GRAMMAR_REGISTRY_LOCALE_TAGS"]
