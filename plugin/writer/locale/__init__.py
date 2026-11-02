# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locale and grammar checking subsystem for WriterAgent / LibreHarper.

Contains:
- Grammar proofreading pipeline (ai_grammar_proofreader, harper, grammar_worker,
  grammar_work_queue, grammar_proofread_cache, grammar_persistence, grammar_obs).
- Supporting linguistic and locale services (linguistic_index, stop_words,
  langdetect_service, locale_abbrev, grammar_proofread_locale, grammar_proofread_text).
"""
