# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared text splitting for embeddings index (512/64 — trusted venv + host extract)."""
from __future__ import annotations

from typing import Any

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


def _import_splitter() -> Any:
    import importlib

    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    try:
        mod = importlib.import_module("langchain_text_splitters")
    except ImportError as exc:
        raise ImportError(
            "langchain-text-splitters is not installed in the configured Python venv. "
            f"Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc
    return mod.RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", " ", ""],
    )


def split_passage_to_chunk_meta(text: str, base_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one passage into embed-sized chunks with char offsets relative to passage text."""
    stripped = str(text or "").strip()
    if not stripped:
        return []

    if len(stripped) <= CHUNK_SIZE:
        meta = dict(base_meta)
        meta.update({"char_start": 0, "char_end": len(stripped), "text": stripped})
        return [meta]

    splitter = _import_splitter()
    pieces = splitter.split_text(stripped)
    if not pieces:
        return []

    chunks: list[dict[str, Any]] = []
    search_from = 0
    for piece in pieces:
        idx = stripped.find(piece, search_from)
        if idx < 0:
            idx = search_from
        char_start = idx
        char_end = idx + len(piece)
        search_from = max(0, char_end - CHUNK_OVERLAP)
        meta = dict(base_meta)
        meta.update({"char_start": char_start, "char_end": char_end, "text": piece})
        chunks.append(meta)
    return chunks


__all__ = ["CHUNK_OVERLAP", "CHUNK_SIZE", "split_passage_to_chunk_meta"]
