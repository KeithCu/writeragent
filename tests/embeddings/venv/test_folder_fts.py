# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.folder_fts."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from plugin.embeddings.venv import folder_fts


def test_build_match_query_single_word():
    assert folder_fts.build_match_query("numpy") == '"numpy"'


def test_build_match_query_multi_word_near():
    q = folder_fts.build_match_query("web search", near_slop=10)
    assert q.startswith("NEAR(")
    assert '"web"' in q
    assert '"search"' in q
    assert ", 10)" in q


def test_build_match_query_near_slash_syntax():
    q = folder_fts.build_match_query("ocean NEAR/3 warming", near_slop=10)
    assert "NEAR(" in q
    assert ", 3)" in q


def test_build_match_query_empty_raises():
    with pytest.raises(ValueError, match="query"):
        folder_fts.build_match_query("   ")


def test_strip_fts_snippet_markers():
    assert folder_fts.strip_fts_snippet_markers("[web] [search] tool-call") == "web search tool-call"
    assert folder_fts.strip_fts_snippet_markers("…[WEB]_[SEARCH]…") == "…WEB_SEARCH…"


def test_maintain_and_search_cold(tmp_path):
    listing = tmp_path / "writing"
    listing.mkdir()
    odt = listing / "sample.odt"
    _write_minimal_odt(odt, ["Hello world paragraph", "web search feature"])

    result = folder_fts.maintain_folder_fts(str(listing), mode="cold")
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] == 2

    db_path = listing / "writeragent_embeddings" / "corpus.db"
    assert db_path.is_file()

    search = folder_fts.search_folder_fts(str(db_path), "web search", k=5, near_slop=10)
    assert search["hits"]
    assert any("web" in (h.get("snippet") or "").lower() for h in search["hits"])
    for hit in search["hits"]:
        snippet = hit.get("snippet") or ""
        assert "[" not in snippet
        assert "]" not in snippet


def test_fts_stats(tmp_path):
    listing = tmp_path / "docs"
    listing.mkdir()
    _write_minimal_odt(listing / "a.odt", ["alpha beta"])
    folder_fts.maintain_folder_fts(str(listing), mode="cold")
    db = listing / "writeragent_embeddings" / "corpus.db"
    meta = listing / "writeragent_embeddings" / "corpus_meta.json"
    stats = folder_fts.fts_stats(str(db), str(meta))
    assert stats["row_count"] >= 1
    assert stats["schema_version"] == "5"


def test_maintain_and_search_ods_cold(tmp_path: Path):
    listing = tmp_path / "reporting"
    listing.mkdir()
    from tests.scripting.ods_fixtures import write_budget_ods

    write_budget_ods(listing / "Budget.ods")

    result = folder_fts.maintain_folder_fts(str(listing), mode="cold")
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] >= 1

    db_path = listing / "writeragent_embeddings" / "corpus.db"
    assert db_path.is_file()

    search = folder_fts.search_folder_fts(str(db_path), "Q4 revenue", k=5, near_slop=10)
    assert search["hits"]
    assert any((h.get("doc_url") or "").endswith("Budget.ods") for h in search["hits"])
    assert any("Revenue" in (h.get("snippet") or "") for h in search["hits"])


def test_maintain_and_search_odp_cold(tmp_path: Path):
    listing = tmp_path / "slides"
    listing.mkdir()
    from tests.scripting.odp_fixtures import write_deck_odp

    write_deck_odp(listing / "deck.odp", body="Q4 Revenue growth")

    result = folder_fts.maintain_folder_fts(str(listing), mode="cold")
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] >= 1

    db_path = listing / "writeragent_embeddings" / "corpus.db"
    assert db_path.is_file()

    search = folder_fts.search_folder_fts(str(db_path), "Q4 revenue", k=5, near_slop=10)
    assert search["hits"]
    assert any((h.get("doc_url") or "").endswith("deck.odp") for h in search["hits"])
    assert any("Revenue" in (h.get("snippet") or "") for h in search["hits"])


def _write_minimal_odt(path: Path, paragraphs: list[str]) -> None:
    import zipfile

    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>"
        + "".join(f'<text:p>{p}</text:p>' for p in paragraphs)
        + "</office:text></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", content_xml)
