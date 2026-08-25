# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for pure chatbot helpers (chat_sidebar_mode, skills, web_research_cache)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.deal_shim import DEAL_MAX_TOKEN
from plugin.chatbot.chat_sidebar_mode import (
    SidebarModeFlags,
    sidebar_mode_flags_for_doc_type,
    get_mode_labels,
    mode_from_label,
    _VALID_MODES,
)
from plugin.chatbot.skills import HUMANIZER_GUIDANCE
from plugin.chatbot.research_cache_fluff import translated_research_cache_fluff
from plugin.chatbot.web_research_cache import (
    snowball_lang_from_locale_tag,
    format_research_cache_key,
    parse_research_cache_key,
    jaccard,
    research_cache_similarity,
)


@given(doc_type=st.sampled_from(["writer", "calc", "draw", "impress", "unknown", ""]))
@settings(max_examples=100)
def test_sidebar_mode_flags_contracts(doc_type: str) -> None:
    flags = sidebar_mode_flags_for_doc_type(doc_type)
    assert isinstance(flags, SidebarModeFlags)
    if doc_type == "writer":
        assert flags.include_brainstorming is True
        assert flags.include_writing_plan is True
        assert flags.include_ppt_master is False
    elif doc_type in ("draw", "impress"):
        assert flags.include_brainstorming is False
        assert flags.include_writing_plan is False
        assert flags.include_ppt_master is True


@given(label=st.text(max_size=DEAL_MAX_TOKEN))
def test_mode_from_label_contracts(label: str) -> None:
    mode = mode_from_label(label)
    assert mode in _VALID_MODES

    labels = get_mode_labels()
    assert isinstance(labels, tuple)
    assert len(labels) >= 5
    assert "librarian" in _VALID_MODES


def test_skills_guidance_constant() -> None:
    assert isinstance(HUMANIZER_GUIDANCE, str)
    assert len(HUMANIZER_GUIDANCE) > 100
    assert "HUMANIZER GUIDANCE" in HUMANIZER_GUIDANCE


def test_research_cache_fluff_tuple() -> None:
    fluff = translated_research_cache_fluff()
    assert isinstance(fluff, tuple)
    assert len(fluff) > 10
    assert "summary" in fluff or "about" in fluff


@given(tag=st.text())
def test_snowball_lang_from_locale_tag_contracts(tag: str) -> None:
    lang = snowball_lang_from_locale_tag(tag)
    assert isinstance(lang, str)
    assert len(lang) > 0


@given(lang=st.sampled_from(["english", "german", "french", "spanish", "unknown"]), key=st.text())
def test_research_cache_key_roundtrip(lang: str, key: str) -> None:
    formatted = format_research_cache_key(lang, key)
    assert isinstance(formatted, str)
    
    parsed_lang, parsed_key = parse_research_cache_key(formatted)
    assert isinstance(parsed_lang, str)
    assert isinstance(parsed_key, str)


@given(stems_a=st.sets(st.text()), stems_b=st.sets(st.text()))
def test_jaccard_and_similarity_mathematical_bounds(stems_a: set[str], stems_b: set[str]) -> None:
    j_val = jaccard(stems_a, stems_b)
    assert isinstance(j_val, float)
    assert 0.0 <= j_val <= 1.0

    sim_val = research_cache_similarity(stems_a, stems_b)
    assert isinstance(sim_val, float)
    assert 0.0 <= sim_val <= 1.0

    if stems_a and stems_b and (stems_a & stems_b):
        assert sim_val > 0.0
