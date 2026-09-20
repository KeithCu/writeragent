# WriterAgent tests for eval-2 Long Writer pack writer prompt
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native pack: no fake GDPval id; prompt stays product-internal-free."""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO = Path(__file__).resolve().parents[2]
_EXP = _REPO / "docs" / "eval" / "eval-2" / "long-writer-pack"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL = _REPO / "docs" / "eval" / "gdpval"

_BANNED_STEERING = (
    "don't invent",
    "do not invent",
    "don't make up",
    "from knowledge",
    "web not required",
    "already named",
)
_BANNED_PRODUCT_INTERNALS = (
    "peer_inner",
    "send_peer_work",
    "send_peer_result",
    "fill_draw_fields",
    "document_research",
    "specialized_workflow_finished",
)
_READY_OR_STUB_GOLD_IDS = (
    "ed2bc14c-99ac-4a2a-8467-482a1a5d67f3",
    "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0",
    "83d10b06-26d1-4636-a32c-23f92c57f30b",
    "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
    "c3525d4d-2012-45df-853e-2d2a0e902991",
    "5f6c57dd-feb6-4e70-b152-4969d92d1608",
    "a46d5cd2-55fe-48fa-a4c6-6aaf6b9991b5",
    "8a7b6fca-60cc-4ae3-b649-971753cbf8b9",
    "4520f882-715a-482d-8e87-1cb3cbdfe975",
)


def test_source_is_native_and_does_not_invent_gdpval_id() -> None:
    source = (_EXP / "SOURCE.md").read_text(encoding="utf-8")
    assert "WriterAgent-native" in source
    assert "Needs gold materials" not in source
    assert "TODO — not in-repo" not in source
    assert "do not invent a fake" in source.lower() or "Do not invent a fake" in source
    assert "download from hugging face" in source.lower()
    assert "huggingface.co" not in source.lower()
    assert "220-row" not in source
    assert "8314d1b1" not in source
    assert "0353ee0c" not in source
    assert "Clarivon" not in source
    assert "PACT Act" not in source
    # Catalog short prefixes of the nine in-repo trees may appear as rejected
    # rows. Claiming any of those trees as this slot's gold path is not allowed.
    for gold_id in _READY_OR_STUB_GOLD_IDS:
        assert f"docs/eval/gdpval/{gold_id}/" not in source, gold_id
    assert not (_EXP / "prompt.gdpval.txt").exists()
    # No new gold tree was added for a fabricated id.
    native_trees = [
        path
        for path in _GDPVAL.iterdir()
        if path.is_dir() and path.name not in _READY_OR_STUB_GOLD_IDS
    ]
    assert native_trees == []


def test_writer_prompt_uses_open_writer_doc_not_microsoft_word() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    assert writer.lstrip().startswith("You are the capital-program officer")
    assert "this open Writer document" in writer
    assert "This Writer document is already open" in writer
    assert "Microsoft Word" not in writer
    assert "word document" not in writer.lower()
    assert "Northhaven Library Program Facts" in writer
    assert "Northhaven Decision Log" in writer
    assert "table of contents" in writer
    assert "heading styles" in writer
    assert "review comments" in writer


def test_writer_prompt_keeps_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "Northhaven",
        "Civic Library",
        "CL-2026-ANNEX",
        "Purpose",
        "Scope of Work",
        "Budget and Schedule",
        "Public-Access Impacts",
        "Open Decisions",
        "annex siting",
        "funding split",
        "weekend staffing",
    ):
        assert heading in text, heading
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned


def test_fixtures_exist_and_keep_identity_anchors() -> None:
    facts = _EXP / "fixtures" / "Northhaven Library Program Facts.odt"
    decisions = _EXP / "fixtures" / "Northhaven Decision Log.odt"
    assert facts.is_file()
    assert decisions.is_file()
    assert not (_EXP / "fixtures" / ".gitkeep").exists()

    def _text(path: Path) -> str:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("content.xml"))
        return " ".join(root.itertext())

    facts_text = _text(facts)
    assert "CL-2026-ANNEX" in facts_text
    assert "Civic Library" in facts_text
    assert "14 Willow Street" in facts_text
    assert "$4.2 million" in facts_text
    assert "$1.8 million" in facts_text
    assert "$2.4 million" in facts_text
    assert "April 2027" in facts_text
    assert "October 2028" in facts_text
    decisions_text = _text(decisions)
    assert "Option A" in decisions_text
    assert "60/40" in decisions_text
    assert "weekend staffing" in decisions_text.lower() or "Weekend staffing" in decisions_text
