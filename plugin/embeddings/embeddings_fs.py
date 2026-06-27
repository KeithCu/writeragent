# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Writer/Calc/Impress/Draw ODF and Microsoft Office extract for embeddings / FTS (no UNO)."""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

log = logging.getLogger(__name__)

TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

WRITER_EXTENSIONS = frozenset({".odt", ".ott", ".fodt"})
CALC_EXTENSIONS = frozenset({".ods", ".ots", ".fods"})
DRAW_EXTENSIONS = frozenset({".odp", ".otp", ".fodp", ".odg"})
INDEXABLE_EXTENSIONS = WRITER_EXTENSIONS | CALC_EXTENSIONS | DRAW_EXTENSIONS

FOREIGN_WRITER_EXTENSIONS = frozenset({".docx", ".doc", ".rtf", ".txt"})
FOREIGN_CALC_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv"})
FOREIGN_DRAW_EXTENSIONS = frozenset({".pptx", ".ppt"})
FOREIGN_EXTENSIONS = FOREIGN_WRITER_EXTENSIONS | FOREIGN_CALC_EXTENSIONS | FOREIGN_DRAW_EXTENSIONS

ALL_INDEXABLE_EXTENSIONS = INDEXABLE_EXTENSIONS | FOREIGN_EXTENSIONS

PROSE_CHUNK_EXTENSIONS = WRITER_EXTENSIONS | frozenset({".docx", ".doc", ".rtf", ".txt"})


@dataclasses.dataclass(frozen=True)
class ParagraphChunk:
    doc_url: str
    para_index: int
    char_start: int
    char_end: int
    text: str
    content_hash: str
    file_mtime: float
    doc_path: str = ""


@dataclasses.dataclass(frozen=True)
class WriterFileEntry:
    path: str
    url: str
    modified: float
    name: str


def content_hash(text: str) -> str:
    """SHA-256 of normalized passage/chunk text (stable for incremental invalidation)."""
    normalized = str(text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def path_to_file_url(path: str) -> str:
    """Build a LO-compatible file URL (file:/// on Unix)."""
    norm = _normalize_path(path)
    quoted = urllib.request.pathname2url(norm)
    if quoted.startswith("/"):
        return "file://" + quoted
    return "file:" + quoted


def _paragraph_texts_from_xml_root(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for el in root.iter(f"{TEXT_NS}p"):
        text = "".join(el.itertext()).strip()
        if text:
            texts.append(text)
    return texts


def extract_writer_paragraphs(path: str) -> list[str]:
    """Read body paragraph text from a Writer .odt/.ott (zip) or .fodt (flat XML)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in WRITER_EXTENSIONS:
        return []
    try:
        if ext == ".fodt":
            root = ET.parse(path).getroot()
            return _paragraph_texts_from_xml_root(root)
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("content.xml"))
        return _paragraph_texts_from_xml_root(root)
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError):
        log.debug("extract_writer_paragraphs failed for %s", path, exc_info=True)
        return []


def _extract_foreign_passages(path: str, ext: str) -> list[str]:
    from plugin.embeddings.venv import embeddings_ooxml_extract as ooxml

    if ext == ".docx":
        return ooxml.extract_docx_paragraphs(path)
    if ext in {".xlsx", ".xls"}:
        return ooxml.extract_spreadsheet_rows(path)
    if ext == ".csv":
        return ooxml.extract_csv_rows(path)
    if ext == ".txt":
        return ooxml.extract_plaintext_paragraphs(path)
    if ext == ".rtf":
        return ooxml.extract_rtf_paragraphs(path)
    if ext == ".pptx":
        return ooxml.extract_pptx_passages(path)
    return []


def _extract_legacy_via_soffice(path: str, ext: str) -> list[str]:
    from plugin.embeddings.embeddings_soffice_convert import LEGACY_BINARY_EXTENSIONS, temporary_converted_odf

    if ext not in LEGACY_BINARY_EXTENSIONS:
        return []
    with temporary_converted_odf(path) as converted:
        if converted is None:
            return []
        return extract_indexable_passages(str(converted))


def extract_indexable_passages(path: str) -> list[str]:
    """Extract indexable passage text from ODF, Microsoft Office, or plain-text files on disk."""
    ext = os.path.splitext(path)[1].lower()
    if ext in WRITER_EXTENSIONS:
        return extract_writer_paragraphs(path)
    if ext in CALC_EXTENSIONS:
        from plugin.embeddings.venv.embeddings_odf_extract import extract_calc_rows

        return extract_calc_rows(path)
    if ext in DRAW_EXTENSIONS:
        from plugin.embeddings.venv.embeddings_odf_extract import extract_draw_pages

        return extract_draw_pages(path)
    if ext in FOREIGN_EXTENSIONS:
        passages = _extract_foreign_passages(path, ext)
        if passages:
            return passages
        return _extract_legacy_via_soffice(path, ext)
    return []


def guess_indexable_paths(directory: str) -> list[WriterFileEntry]:
    """List indexable document siblings in *directory* (stdlib scan, no UNO)."""
    listing_root = _normalize_path(directory)
    entries: list[WriterFileEntry] = []
    try:
        names = sorted(os.listdir(listing_root))
    except OSError:
        log.debug("guess_indexable_paths listdir failed for %s", listing_root, exc_info=True)
        return []
    for name in names:
        full = os.path.join(listing_root, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALL_INDEXABLE_EXTENSIONS:
            continue
        try:
            mtime = float(os.path.getmtime(full))
        except OSError:
            mtime = 0.0
        norm = _normalize_path(full)
        entries.append(
            WriterFileEntry(
                path=norm,
                url=path_to_file_url(norm),
                modified=mtime,
                name=name,
            )
        )
    return entries


def guess_writer_paths(directory: str) -> list[WriterFileEntry]:
    """Alias for :func:`guess_indexable_paths`."""
    return guess_indexable_paths(directory)


def path_uses_prose_chunking(path: str) -> bool:
    """Return True when index chunks should use sentence splitting (Writer-style prose)."""
    ext = os.path.splitext(path)[1].lower()
    return ext in PROSE_CHUNK_EXTENSIONS


def indexable_chunks_from_path(
    path: str,
    *,
    doc_url: str | None = None,
    file_mtime: float | None = None,
) -> tuple[int, list[ParagraphChunk]]:
    """Extract native passages, split to embed chunks; return (passage_count, chunk_rows)."""
    from plugin.embeddings.embeddings_split import split_passage_to_chunk_meta

    norm = _normalize_path(path)
    url = doc_url if doc_url else path_to_file_url(norm)
    try:
        mtime = float(file_mtime if file_mtime is not None else os.path.getmtime(norm))
    except OSError:
        mtime = 0.0

    passages = [text.strip() for text in extract_indexable_passages(norm) if text.strip()]
    prose = path_uses_prose_chunking(norm)
    chunks: list[ParagraphChunk] = []
    for para_index, passage in enumerate(passages):
        base_meta = {
            "doc_url": url,
            "para_index": para_index,
            "file_mtime": mtime,
        }
        for piece in split_passage_to_chunk_meta(passage, base_meta, prose=prose):
            piece_text = str(piece.get("text") or "").strip()
            if not piece_text:
                continue
            chunks.append(
                ParagraphChunk(
                    doc_url=url,
                    para_index=para_index,
                    char_start=int(piece.get("char_start") or 0),
                    char_end=int(piece.get("char_end") or len(piece_text)),
                    text=piece_text,
                    content_hash=content_hash(piece_text),
                    file_mtime=mtime,
                    doc_path=norm,
                )
            )
    return len(passages), chunks


def paragraph_chunks_from_path(path: str, *, doc_url: str | None = None, file_mtime: float | None = None) -> list[ParagraphChunk]:
    """Build embed-sized chunk rows from one supported document on disk."""
    _passage_count, chunks = indexable_chunks_from_path(path, doc_url=doc_url, file_mtime=file_mtime)
    del _passage_count
    return chunks


def chunk_to_index_row(chunk: ParagraphChunk, *, chunk_id: int | None = None) -> dict[str, Any]:
    """Dict shape for venv index_paragraphs / ingest."""
    row: dict[str, Any] = {
        "doc_url": chunk.doc_url,
        "para_index": chunk.para_index,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "content_hash": chunk.content_hash,
        "text": chunk.text,
        "file_mtime": chunk.file_mtime,
    }
    if chunk_id is not None:
        row["chunk_id"] = chunk_id
    return row
