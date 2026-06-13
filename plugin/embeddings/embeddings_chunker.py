# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Paragraph-level chunk extraction for embeddings indexing — re-exports stdlib ODF path."""
from __future__ import annotations

from plugin.embeddings.embeddings_fs import (
    DRAW_EXTENSIONS,
    ParagraphChunk,
    WriterFileEntry,
    chunk_to_index_row,
    content_hash,
    extract_indexable_passages,
    extract_writer_paragraphs,
    guess_indexable_paths,
    guess_writer_paths,
    indexable_chunks_from_path,
    paragraph_chunks_from_path,
    path_to_file_url,
)

__all__ = [
    "DRAW_EXTENSIONS",
    "ParagraphChunk",
    "WriterFileEntry",
    "chunk_to_index_row",
    "content_hash",
    "extract_indexable_passages",
    "extract_writer_paragraphs",
    "guess_indexable_paths",
    "guess_writer_paths",
    "indexable_chunks_from_path",
    "list_indexable_sibling_files",
    "paragraph_chunks_from_path",
    "path_to_file_url",
]


def list_indexable_sibling_files(ctx, model):  # noqa: ANN001 — UNO host compat for tests
    """Return office siblings in the active folder (host UNO listing — deprecated for indexing)."""
    from plugin.doc.document_research import FileEntry, list_nearby_files

    listing = list_nearby_files(ctx, model, file_kind="documents")
    if listing.get("status") != "ok":
        return [], listing.get("message", "Could not list nearby files")
    files: list[FileEntry] = list(listing.get("files") or [])
    return files, None
