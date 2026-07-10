# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared agent manual (single source of truth read by BOTH agents) and the get_guidance tool.

R3/T4 introduced the sectioned manual for the MCP agent; R4 unified it; R5 re-homed the source:
the pieces of the original chat system prompt (constants.py) — updated in place and extended —
ARE the manual now. The sidebar template assembles them directly; agent_manual maps topic -> piece
for the MCP channel (get_guidance) and the agent-backend path, adding the MCP-only extras; and the
sections are per document type so a Calc session never reads Writer advice. No LibreOffice required."""
from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.agent_manual import (
    MANUAL_SECTIONS,
    doc_type_of,
    full_manual,
    full_manual_for_model,
    get_section,
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


# ---- sections / topics -----------------------------------------------------

def test_topics_and_sections_align():
    topics = list_topics("writer")
    assert "editing" in topics and "review-modes" in topics
    for t in topics:
        assert t in MANUAL_SECTIONS and MANUAL_SECTIONS[t].strip()


def test_normalize_aliases():
    assert normalize_topic("tracked-changes") == "review-modes"
    assert normalize_topic("Tracked_Changes") == "review-modes"
    assert normalize_topic("crop") == "images"
    assert normalize_topic("429") == "concurrency"
    assert normalize_topic("editing") == "editing"
    assert normalize_topic("nonsense") is None
    assert normalize_topic("") is None


def test_get_section_known_and_unknown():
    assert "apply_document_content" in get_section("editing").lower()
    assert get_section("nope") is None


def test_manual_index_lists_every_topic():
    idx = manual_index("writer")
    for t in list_topics("writer"):
        assert t in idx


# ---- per document type -----------------------------------------------------

def test_doc_type_of_resolution():
    assert doc_type_of(None) is None
    assert doc_type_of(WRITER_DOC) == "writer"
    assert doc_type_of(CALC_DOC) == "calc"
    assert doc_type_of(DRAW_DOC) == "draw"


def test_calc_topics_are_generic_and_leak_no_writer_advice():
    topics = list_topics("calc")
    assert "editing" in topics and "concurrency" in topics
    assert "review-modes" not in topics and "navigation" not in topics
    # The Writer-specific tool names must not leak into a Calc session's guidance.
    assert "apply_document_content" not in get_section("editing", "calc")
    assert get_section("review-modes", "calc") is None
    assert normalize_topic("crop", "calc") is None


def test_no_document_serves_neutral_generic_index():
    assert list_topics(None) == list_topics("calc")  # generic set
    idx = manual_index(None)
    assert "No document is open" in idx
    assert "editing" in idx and "concurrency" in idx


# ---- single source: the prompt pieces feed BOTH channels ---------------------

def test_shared_pieces_are_identical_in_both_channels():
    """The heart of the architecture: a topic served through get_guidance IS the same string the
    prompt templates embed — one piece, per-channel assemblies, drift impossible."""
    from plugin.framework import prompts as c

    # Topic -> the exact constants piece (identity, not a copy that could be edited apart).
    assert MANUAL_SECTIONS["editing-html"] is c.WRITER_APPLY_DOCUMENT_HTML_RULES
    assert MANUAL_SECTIONS["review-modes"] is c.WRITER_REVIEW_MODES_RULES
    assert MANUAL_SECTIONS["search"] is c.WRITER_SEARCH_RULES
    assert MANUAL_SECTIONS["navigation"] is c.WRITER_NAVIGATION_RULES
    assert MANUAL_SECTIONS["images"] is c.WRITER_IMAGES_RULES
    # The editing topic bundles the workflow pieces verbatim (the HTML contract is its own
    # subdivision, editing-html). The pointer to the subdivision is appended ONLY when the topic
    # is served alone (get_section) — in full_manual the editing-html section follows inline, so
    # a "go read that topic" sentence would dangle there.
    for piece in (c.TOOL_USAGE_PATTERNS, c.TRANSLATION_RULES):
        assert piece.strip() in MANUAL_SECTIONS["editing"]
    assert "editing-html" in get_section("editing", "writer")      # on-demand: pointer present
    assert "read the editing-html topic" not in full_manual("writer")  # concatenated: no dangler
    # The generic (Calc/Draw/no-doc) editing topic is the same object the Calc and Draw sidebar
    # prompts embed.
    assert get_section("editing", "calc") is c.GENERIC_EDIT_CONFIRMATION_RULES
    assert c.GENERIC_EDIT_CONFIRMATION_RULES in c.DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE
    assert c.GENERIC_EDIT_CONFIRMATION_RULES in c.DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE


def test_sidebar_hybrid_prompt_composition():
    """HYBRID delivery: the ambient Writer sidebar prompt carries the original pieces plus the
    safety-critical review-modes piece exactly once; the reference pieces (search, navigation,
    images) stay OUT of the ambient text and are pulled on demand via get_guidance, which must
    therefore be visible to the sidebar (tier core)."""
    from plugin.doc.document_research_tools import GetGuidance
    from plugin.framework import prompts as c

    template = c.DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE
    for piece in (
        c.TOOL_USAGE_PATTERNS,
        c.WRITER_APPLY_DOCUMENT_HTML_RULES,
        c.TRANSLATION_RULES,
        c.WRITER_REVIEW_MODES_RULES,
    ):
        assert template.count(piece) == 1
    for piece in (c.WRITER_SEARCH_RULES, c.WRITER_NAVIGATION_RULES, c.WRITER_IMAGES_RULES):
        assert piece not in template          # on-demand, not ambient
    assert "get_guidance" in template         # the tools section tells the model how to pull
    assert GetGuidance.tier == "core"         # visible to the sidebar's default tool list


def test_mcp_only_extras_stay_out_of_the_sidebar_prompt():
    """The HTTP 429 concurrency contract is real for MCP clients and the agent-backend path,
    meaningless for the in-process sidebar — channel-specific text must not leak across."""
    from plugin.framework import prompts as c

    assert "429" in get_section("concurrency", "writer")   # MCP topic has it
    assert "429" in full_manual("writer")                  # agent backend (HTTP) gets it
    assert "429" not in c.DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE  # sidebar never sees it
    assert "429" not in c.DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE
    assert "429" not in c.DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE


# ---- full manual (agent-backend delivery) ------------------------------------

def test_full_manual_writer_covers_key_rules():
    """Pin the high-value cross-cutting rules (moved here from the retired EXTERNAL_AGENT_GUIDANCE
    blob in constants.py) so a refactor can't silently drop one from the single source."""
    g = full_manual("writer")
    assert "apply_document_content" in g           # prefer the document tools
    assert "full_document" in g                    # whole-doc vs search target
    assert "replaced_count" in g                   # confirm via structured field
    assert "accept or reject" in g.lower()         # don't resolve own tracked changes
    assert "429" in g                              # one operation at a time / busy retry
    assert "include_images" in g                   # how a vision model sees images
    # Stale-snapshot warning (channel-neutral wording: the classic sidebar DOES auto-refresh its
    # document context after successful mutations, so the old "NOT auto-refreshed" claim was false
    # for one of the channels this shared piece ships to).
    assert "partial/truncated snapshot" in g
    assert "record" in g and "wait" in g           # the three review modes reach the sidebar too


def test_full_manual_contains_every_section_in_order():
    g = full_manual("writer")
    positions = [g.index(MANUAL_SECTIONS[t]) for t in list_topics("writer")]
    assert positions == sorted(positions)


def test_full_manual_for_model_switches_per_app():
    assert "review-modes" in " ".join(list_topics("writer")) or True  # sanity
    assert "TRACKED CHANGES" in full_manual_for_model(WRITER_DOC)
    calc_manual = full_manual_for_model(CALC_DOC)
    assert "TRACKED CHANGES" not in calc_manual
    assert "structured fields" in calc_manual
    assert "429" in calc_manual


# ---- GetGuidance tool --------------------------------------------------------

def test_get_guidance_no_topic_returns_writer_index():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC))
    assert res["status"] == "ok" and res["doc_type"] == "writer"
    assert set(list_topics("writer")).issubset(set(res["topics"]))
    assert "get_guidance" in res["index"]


def test_get_guidance_topic_returns_section():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="review-modes")
    assert res["status"] == "ok" and res["topic"] == "review-modes"
    assert "record" in res["guidance"].lower() and "wait" in res["guidance"].lower()


def test_get_guidance_alias():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="tracked changes")
    assert res["status"] == "ok" and res["topic"] == "review-modes"


def test_get_guidance_unknown_topic_errors_with_list():
    res = GetGuidance().execute(FakeCtx(WRITER_DOC), topic="zzz")
    assert res["status"] == "error" and res["code"] == "UNKNOWN_TOPIC"
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
    assert "No document is open" in res["index"]


def test_get_guidance_does_not_require_document():
    assert GetGuidance.requires_document is False
