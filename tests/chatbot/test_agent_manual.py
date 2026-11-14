# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared agent manual (single source of truth read by BOTH agents) and the get_guidance tool.

R3/T4 introduced the sectioned manual for the MCP agent; R4 unified it; R5 re-homed the source:
the pieces of the original chat system prompt (prompts.py) — updated in place and extended —
ARE the manual now. The sidebar template assembles them directly; agent_manual maps topic -> piece
for the MCP channel (get_guidance) and the agent-backend path, adding the MCP-only extras; and the
sections are per document type so a Calc session never reads Writer advice. No LibreOffice required."""

from plugin.chatbot.agent_manual import (
    _WRITER_TOPICS,
    full_manual,
    get_section,
    list_topic_entries,
    list_topics,
    manual_index,
    normalize_topic,
)
from plugin.doc.document_research_tools import GetGuidance


class FakeDoc:
    """A document model whose supportsService answers like Writer/Calc/Draw."""

    def __init__(self, services=()):
        self._services = set(services)

    def supportsService(self, name):
        return name in self._services


WRITER_DOC = FakeDoc()
CALC_DOC = FakeDoc({"com.sun.star.sheet.SpreadsheetDocument"})
DRAW_DOC = FakeDoc({"com.sun.star.drawing.DrawingDocument"})


class FakeCtx:
    def __init__(self, doc):
        self.doc = doc


def test_list_topic_entries_matches_list_topics():
    for doc_type in ("writer", "calc", "draw", None):
        ids_from_entries = [topic_id for topic_id, _summary in list_topic_entries(doc_type)]
        assert ids_from_entries == list_topics(doc_type)


def test_topics_and_sections_align():
    topics = list_topics("writer")
    assert "editing" in topics and "review-modes" in topics
    for t in topics:
        sec = get_section(t, "writer")
        assert sec and sec.strip()


def test_normalize_aliases():
    assert normalize_topic("tracked-changes") == "review-modes"
    assert normalize_topic("Tracked_Changes") == "review-modes"
    assert normalize_topic("crop") == "images"
    assert normalize_topic("429") == "concurrency"
    assert normalize_topic("editing") == "editing"
    assert normalize_topic("nonsense") is None
    assert normalize_topic("") is None


def test_get_section_known_and_unknown():
    sec = get_section("editing")
    assert sec is not None and sec.strip()
    assert get_section("nope") is None


def test_manual_index_lists_every_topic():
    idx = manual_index("writer")
    for t in list_topics("writer"):
        assert t in idx


# ---- per document type -----------------------------------------------------

def test_calc_topics_use_calc_manual_not_writer():
    from plugin.framework import prompts as c

    topics = list_topics("calc")
    assert topics == ["workflow", "formula-syntax", "directives", "concurrency"]
    assert "review-modes" not in topics and "navigation" not in topics and "editing-html" not in topics
    for topic in topics:
        assert get_section(topic, "calc")
    assert get_section("workflow", "calc") is c.CALC_WORKFLOW
    formula = get_section("formula-syntax", "calc")
    assert formula == c.CALC_FORMULA_SYNTAX_TEMPLATE.replace("{venv_policy}", "")
    assert "{venv_policy}" not in formula
    assert get_section("directives", "calc") is c.CALC_CORE_DIRECTIVES
    assert normalize_topic("crop", "calc") is None


def test_draw_manual_topics_and_impress_fold():
    from plugin.framework import prompts as c

    assert get_section("directives", "draw") is c.DRAW_CORE_DIRECTIVES
    assert get_section("get-image", "draw") is c.DRAW_GET_IMAGE_TOOL_LINE
    assert full_manual("impress") == full_manual("draw")
    assert full_manual("impress") != full_manual("generic")

    res = GetGuidance().execute(FakeCtx(DRAW_DOC))
    assert res["doc_type"] == "draw"
    assert "get-image" in res["topics"]


def test_no_document_serves_neutral_generic_index():
    assert list_topics(None) == list_topics("generic")
    idx = manual_index(None)
    assert "editing" in idx and "concurrency" in idx


# ---- single source: the prompt pieces feed BOTH channels ---------------------

def test_shared_pieces_are_identical_in_both_channels():
    """The heart of the architecture: a topic served through get_guidance IS the same string the
    prompt templates embed — one piece, per-channel assemblies, drift impossible."""
    from plugin.framework import prompts as c

    writer_texts = {topic.id: topic.text for topic in _WRITER_TOPICS}
    # Topic -> the exact constants piece (identity, not a copy that could be edited apart).
    # editing-html = ambient block + pull-only examples; equality, since concatenation makes a new string.
    assert writer_texts["editing-html"] == c.WRITER_APPLY_DOCUMENT_HTML_RULES + c.HTML_MATH_AND_LINK_EXAMPLES
    assert writer_texts["review-modes"] is c.WRITER_REVIEW_MODES_RULES
    assert writer_texts["search"] is c.WRITER_SEARCH_RULES
    assert writer_texts["navigation"] is c.WRITER_NAVIGATION_RULES
    assert writer_texts["images"] is c.WRITER_IMAGES_RULES
    # The editing topic bundles the workflow pieces verbatim (the HTML contract is its own
    # subdivision, editing-html). The pointer to the subdivision is appended ONLY when the topic
    # is served alone (get_section) — in full_manual the editing-html section follows inline, so
    # a "go read that topic" sentence would dangle there.
    for piece in (c.CONFIRM_EDITS_FROM_STRUCTURED_FIELDS, c.TOOL_USAGE_PATTERNS, c.TRANSLATION_RULES):
        assert piece.strip() in writer_texts["editing"]
    assert "editing-html" in get_section("editing", "writer")      # on-demand: pointer present
    assert "read the editing-html topic" not in full_manual("writer")  # concatenated: no dangler
    # The generic editing topic remains in Calc and Draw chat templates (not the app manuals).
    assert c.GENERIC_EDIT_CONFIRMATION_RULES in c.DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE
    assert c.GENERIC_EDIT_CONFIRMATION_RULES in c.DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE


# ---- full manual (agent-backend delivery) ------------------------------------

def test_full_manual_contains_every_section_in_order():
    g = full_manual("writer")
    writer_texts = {topic.id: topic.text for topic in _WRITER_TOPICS}
    positions = [g.index(writer_texts[t]) for t in list_topics("writer")]
    assert positions == sorted(positions)


def test_full_manual_impress_uses_draw_sections():
    assert full_manual("impress") == full_manual("draw")
    assert full_manual("Impress") == full_manual("draw")
    assert full_manual("  IMPRESS ") == full_manual("draw")
    assert list_topics("Impress") == list_topics("draw")
    assert full_manual(" Writer ") == full_manual("writer")
    assert full_manual(" Nope ") == full_manual("generic")


# ---- GetGuidance tool --------------------------------------------------------

def test_get_guidance_no_topic_returns_writer_index():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC))
    assert res["status"] == "ok" and res["doc_type"] == "writer"
    assert set(list_topics("writer")).issubset(set(res["topics"]))
    assert "get_guidance" in res["index"]
    assert "current_local_datetime" in res
    assert "Current local date and time:" in res["current_local_datetime"]


def test_get_guidance_topic_returns_section():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="review-modes")
    assert res["status"] == "ok" and res["topic"] == "review-modes"
    assert res["guidance"] == get_section("review-modes", "writer")


def test_get_guidance_alias():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="tracked changes")
    assert res["status"] == "ok" and res["topic"] == "review-modes"


def test_get_guidance_unknown_topic_errors_with_list():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="zzz")
    assert res["status"] == "error" and res["code"] == "UNKNOWN_TOPIC"
    assert "topics" in res
    assert "details" not in res
    assert "editing" in res["topics"]


def test_get_guidance_calc_session_never_reads_writer_advice():
    res = GetGuidance().execute(FakeCtx(CALC_DOC))
    assert res["doc_type"] == "calc"
    assert "review-modes" not in res["topics"]
    sec = GetGuidance().execute(FakeCtx(CALC_DOC), topic="review-modes")
    assert sec["status"] == "error"


def test_get_guidance_without_document_is_neutral():
    res = GetGuidance().execute(FakeCtx(None))
    assert res["status"] == "ok" and res["doc_type"] is None
    assert res["topics"] == list_topics("generic")


def test_no_duplicate_aliases():
    from plugin.chatbot.agent_manual import _ALIASES_BY_TOPIC

    seen: set[str] = set()
    for aliases in _ALIASES_BY_TOPIC.values():
        for alias in aliases:
            assert alias not in seen, f"Duplicate alias: {alias}"
            seen.add(alias)


def test_manual_topics_and_aliases_consistency():
    from plugin.chatbot.agent_manual import _ALIASES_BY_TOPIC, _MANUALS, normalize_topic

    for app_name, topics in _MANUALS.items():
        seen_ids: set[str] = set()
        for t in topics:
            assert t.id and t.summary, f"Topic missing id or summary in {app_name}: {t}"
            assert t.id not in seen_ids, f"Duplicate topic {t.id} in {app_name}"
            seen_ids.add(t.id)

    writer_ids = {t.id for t in _MANUALS["writer"]}
    for canon_id in _ALIASES_BY_TOPIC:
        assert canon_id in writer_ids, f"Alias key {canon_id} not in writer topics"

    generic_ids = {t.id for t in _MANUALS["generic"]}
    assert generic_ids.issubset(writer_ids), (
        "Generic topic ids must be a subset of writer topic ids"
    )

    for app_name, topics in _MANUALS.items():
        app_topic_ids = {t.id for t in topics}
        for canon_id, alias_tuple in _ALIASES_BY_TOPIC.items():
            if canon_id in app_topic_ids:
                for alias in alias_tuple:
                    assert normalize_topic(alias, app_name) == canon_id, (
                        f"Alias {alias} did not resolve to {canon_id} in {app_name}"
                    )


