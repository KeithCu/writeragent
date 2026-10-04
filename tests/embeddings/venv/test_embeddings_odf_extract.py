# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_odf_extract."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from plugin.embeddings import embeddings_fs
from plugin.embeddings.venv import embeddings_odf_extract
from tests.scripting.odp_fixtures import write_deck_odp, write_drawing_odg
from tests.scripting.ods_fixtures import write_budget_ods

pytest.importorskip("odf")


def test_extract_draw_pages_from_odp(tmp_path: Path):
    odp = tmp_path / "deck.odp"
    write_deck_odp(odp, body="Q4 Revenue", notes="Speaker note about revenue")
    passages = embeddings_odf_extract.extract_draw_pages(str(odp))
    assert len(passages) == 2
    assert passages[0].startswith("[Slide: Intro]\t")
    assert "Q4 Revenue" in passages[0]
    assert passages[1].startswith("[Notes: Intro]\t")
    assert "Speaker note about revenue" in passages[1]


def test_extract_draw_pages_from_odg(tmp_path: Path):
    odg = tmp_path / "fig.odg"
    write_drawing_odg(odg, body="Diagram label")
    passages = embeddings_odf_extract.extract_draw_pages(str(odg))
    assert len(passages) == 1
    assert passages[0].startswith("[Slide: Layer1]\t")
    assert "Diagram label" in passages[0]


def test_paragraph_chunks_from_odp_path(tmp_path: Path):
    odp = tmp_path / "deck.odp"
    write_deck_odp(odp)
    chunks = embeddings_fs.paragraph_chunks_from_path(str(odp))
    assert len(chunks) == 1
    assert chunks[0].para_index == 0
    assert chunks[0].content_hash == embeddings_fs.content_hash(chunks[0].text)
    assert "Revenue" in chunks[0].text


def test_extract_draw_pages_missing_odfpy(tmp_path: Path):
    odp = tmp_path / "deck.odp"
    write_deck_odp(odp)
    real_import = __import__

    def _fake_import(name, *args, **kwargs):
        if name == "odf.draw" or name == "odf.opendocument" or name == "odf.presentation":
            raise ImportError("no odf")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_fake_import):
        with pytest.raises(RuntimeError):
            embeddings_odf_extract.extract_draw_pages(str(odp))


def test_extract_calc_rows_from_ods(tmp_path: Path):
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    rows = embeddings_odf_extract.extract_calc_rows(str(ods))
    assert len(rows) == 1
    assert rows[0].startswith("[Sheet: Budget]\t")
    assert "Q4" in rows[0]
    assert "Revenue" in rows[0]
    assert "1200000" in rows[0]


def test_paragraph_chunks_from_ods_path(tmp_path: Path):
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    chunks = embeddings_fs.paragraph_chunks_from_path(str(ods))
    assert len(chunks) == 1
    assert chunks[0].para_index == 0
    assert chunks[0].content_hash == embeddings_fs.content_hash(chunks[0].text)
    assert "Revenue" in chunks[0].text


def test_extract_calc_rows_missing_pandas(tmp_path: Path):
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    real_import = __import__

    def _fake_import(name, *args, **kwargs):
        if name == "pandas":
            raise ImportError("no pandas")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_fake_import):
        with pytest.raises(RuntimeError):
            embeddings_odf_extract.extract_calc_rows(str(ods))
