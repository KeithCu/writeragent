# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

from plugin.embeddings.venv import embeddings_folder_maintain


def _write_min_odt(path: Path, text: str = "Hello") -> None:
    content_xml = f"""<?xml version="1.0"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:text><text:p>{text}</text:p></office:text></office:body>
</office:document-content>""".encode()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", content_xml)


def test_maintain_cold_calls_heartbeat_and_ingest(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc)
    heartbeats: list[dict] = []

    with patch("plugin.embeddings.venv.embeddings_folder_maintain.index_is_empty", return_value=True):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain.needs_cold_rebuild", return_value=True):
            with patch("plugin.embeddings.venv.embeddings_folder_maintain.clear_folder_cache") as clear_mock:
                with patch(
                    "plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows",
                    return_value={"upserted": 1},
                ) as ingest_mock:
                    result = embeddings_folder_maintain.maintain_folder_index(
                        str(tmp_path),
                        embedding_model="all-MiniLM-L6-v2",
                        mode="cold",
                        heartbeat_fn=heartbeats.append,
                    )
    clear_mock.assert_called_once()
    ingest_mock.assert_called_once()
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] == 1
    assert any(h.get("phase") == "start" for h in heartbeats)
    assert any(h.get("phase") == "done" for h in heartbeats)


def test_maintain_incremental_skips_fresh_file(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc, "same")
    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        '{"schema_version":"3","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )

    from plugin.embeddings.embeddings_cache import mark_file_indexed
    from plugin.embeddings.embeddings_fs import content_hash, path_to_file_url

    doc_url = path_to_file_url(str(doc))
    mark_file_indexed(
        db_path,
        doc_url,
        doc.stat().st_mtime,
        indexed_at=doc.stat().st_mtime,
        paragraphs={"0": content_hash("same")},
    )

    with patch("plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows") as ingest_mock:
        result = embeddings_folder_maintain.maintain_folder_index(
            str(tmp_path),
            embedding_model="all-MiniLM-L6-v2",
            mode="incremental",
        )
    ingest_mock.assert_not_called()
    assert result["mode"] == "incremental"
    assert result["indexed_paragraphs"] == 0
