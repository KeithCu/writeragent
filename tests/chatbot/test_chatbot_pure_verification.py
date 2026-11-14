# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Hypothesis verification for pure chatbot helpers (chat_sidebar_mode, web_research_cache)."""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from plugin.chatbot.chat_sidebar_mode import (
    CHAT_MODE_CHAT,
    SidebarModeFlags,
    get_mode_labels,
    mode_from_label,
    sidebar_mode_flags_for_doc_type,
)
from plugin.chatbot.web_research_cache import (
    format_research_cache_key,
    parse_research_cache_key,
    research_cache_similarity,
    snowball_lang_from_locale_tag,
)
from plugin.framework.deal_shim import DEAL_MAX_TOKEN


@pytest.mark.parametrize(
    ("doc_type", "expected"),
    [
        (
            "writer",
            SidebarModeFlags(
                include_brainstorming=True,
                include_writing_plan=True,
                include_ppt_master=False,
                include_deep_research=True,
            ),
        ),
        (
            "calc",
            SidebarModeFlags(
                include_brainstorming=False,
                include_writing_plan=False,
                include_ppt_master=False,
                include_deep_research=True,
            ),
        ),
        (
            "draw",
            SidebarModeFlags(
                include_brainstorming=False,
                include_writing_plan=False,
                include_ppt_master=True,
                include_deep_research=True,
            ),
        ),
        (
            "impress",
            SidebarModeFlags(
                include_brainstorming=False,
                include_writing_plan=False,
                include_ppt_master=True,
                include_deep_research=True,
            ),
        ),
        (
            "unknown",
            SidebarModeFlags(
                include_brainstorming=False,
                include_writing_plan=False,
                include_ppt_master=False,
                include_deep_research=False,
            ),
        ),
        (
            "",
            SidebarModeFlags(
                include_brainstorming=False,
                include_writing_plan=False,
                include_ppt_master=False,
                include_deep_research=False,
            ),
        ),
    ],
)
def test_sidebar_mode_flags_for_doc_type(
    doc_type: str, expected: SidebarModeFlags
) -> None:
    assert sidebar_mode_flags_for_doc_type(doc_type) == expected


@given(label=st.text(max_size=DEAL_MAX_TOKEN))
def test_mode_from_label_unknown_text_falls_back_to_chat(label: str) -> None:
    assume(label.strip() not in get_mode_labels())
    assert mode_from_label(label) == CHAT_MODE_CHAT


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("de_DE", "german"),
        ("fr-CA", "french"),
        ("DE", "german"),
        ("pt_BR", "portuguese"),
        ("", "english"),
        ("xx", "english"),
    ],
)
def test_snowball_lang_from_locale_tag(tag: str, expected: str) -> None:
    assert snowball_lang_from_locale_tag(tag) == expected


@given(
    lang=st.sampled_from(["english", "german", "french", "spanish"]),
    key=st.text(min_size=1),
    mode=st.sampled_from(["", "deep"]),
)
def test_research_cache_key_roundtrip(lang: str, key: str, mode: str) -> None:
    formatted = format_research_cache_key(lang, key, mode=mode)
    assert parse_research_cache_key(formatted) == (lang, key)


@given(stems_a=st.sets(st.text()), stems_b=st.sets(st.text()))
def test_jaccard_and_similarity_mathematical_bounds(
    stems_a: set[str], stems_b: set[str]
) -> None:
    sim_val = research_cache_similarity(stems_a, stems_b)
    assert 0.0 <= sim_val <= 1.0
    assert sim_val == research_cache_similarity(stems_b, stems_a)

    if stems_a:
        assert research_cache_similarity(stems_a, stems_a) == 1.0
    if stems_a and stems_b and not (stems_a & stems_b):
        assert sim_val == 0.0


@pytest.mark.parametrize(
    ("stems_a", "stems_b", "expected"),
    [
        ({"a", "b"}, {"a", "b", "c", "d"}, 1.0),
        ({"a", "b", "c"}, {"b", "c", "d"}, pytest.approx(2 / 3)),
    ],
)
def test_research_cache_similarity_exact(
    stems_a: set[str], stems_b: set[str], expected: object
) -> None:
    assert research_cache_similarity(stems_a, stems_b) == expected
