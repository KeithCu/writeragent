# Tests for web research chat block formatting (no LibreOffice required).

from plugin.modules.chatbot.web_research import _norm_research_query
from plugin.modules.chatbot.web_research_chat import (
    web_research_engine_chat_block,
    web_search_engine_step_chat_text,
)


def test_step_zero_matches_legacy_engine_block():
    q = "best pizza test"
    a = web_search_engine_step_chat_text(q, 0, approval_required=False)
    b = web_research_engine_chat_block(q, approval_required=False)
    assert a == b
    assert "[Web search]" in a
    assert "[Additional web search]" not in a


def test_step_one_is_additional_not_duplicate_full_header():
    q = "refined query"
    first = web_search_engine_step_chat_text(q, 0, approval_required=False)
    second = web_search_engine_step_chat_text(q, 1, approval_required=False)
    assert "[Additional web search]" in second
    assert second.count("[Web search]") == 0
    assert "[Web search]" in first
    assert first != second


def test_step_zero_approval_uses_approval_header():
    q = "x"
    t = web_search_engine_step_chat_text(q, 0, approval_required=True)
    assert "approval" in t.lower() or "Approval" in t


def test_step_index_negative_treated_as_first():
    q = "q"
    assert web_search_engine_step_chat_text(q, -1, approval_required=False) == web_search_engine_step_chat_text(
        q, 0, approval_required=False
    )


def test_norm_research_query_collapses_whitespace_and_case():
    assert _norm_research_query("  Best  Pizza \n") == _norm_research_query("best pizza")
    assert _norm_research_query("") == ""
