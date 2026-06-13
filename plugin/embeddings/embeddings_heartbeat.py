# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared rebuild/index heartbeat line formatting for Search dialog UI."""
from __future__ import annotations


def format_index_heartbeat_line(
    filename: str,
    *,
    paragraphs: int,
    chunks: int,
    elapsed_sec: float,
) -> str:
    """Format one per-file rebuild progress line (native paragraphs + embed chunks)."""
    para_label = "paragraph" if int(paragraphs) == 1 else "paragraphs"
    chunk_label = "chunk" if int(chunks) == 1 else "chunks"
    return f"{filename}: {int(paragraphs)} {para_label}, {int(chunks)} {chunk_label}, {elapsed_sec:.2f}s"


def heartbeat_counts_from_payload(payload: dict) -> tuple[int, int]:
    """Return (paragraphs, chunks) from a maintain heartbeat payload."""
    paragraphs = int(payload.get("paragraphs") or 0)
    chunks = int(payload.get("upserted") or payload.get("chunks") or paragraphs)
    return paragraphs, chunks


__all__ = ["format_index_heartbeat_line", "heartbeat_counts_from_payload"]
