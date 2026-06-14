# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parent-paragraph expansion: merge sub-chunk hits by para_index, return full paragraph text."""
from __future__ import annotations

from typing import Any

from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, paragraph_bodies_for_locators


def _locator_from_candidate(cand: dict[str, Any]) -> tuple[str, int]:
    return (str(cand.get("doc_url") or ""), int(cand.get("para_index") or 0))


def _merge_matched_by(existing: dict[str, Any], incoming: dict[str, Any]) -> list[str] | None:
    left = existing.get("matched_by")
    right = incoming.get("matched_by")
    if not left and not right:
        return None
    merged: set[str] = set()
    for value in (left, right):
        if isinstance(value, (list, tuple, set)):
            merged.update(str(x) for x in value)
        elif value:
            merged.add(str(value))
    return sorted(merged) if merged else None


def merge_candidates_by_paragraph(
    candidates: list[dict[str, Any]],
    paragraph_bodies: dict[tuple[str, int], str],
) -> list[dict[str, Any]]:
    """Collapse sub-chunk candidates to one row per paragraph; keep highest score."""
    if not candidates:
        return []

    best: dict[tuple[str, int], dict[str, Any]] = {}
    for cand in candidates:
        key = _locator_from_candidate(cand)
        merged = dict(cand)
        body = paragraph_bodies.get(key)
        if body:
            merged["snippet"] = body
            merged["parent_expanded"] = True
        score = float(merged.get("score") or 0.0)
        existing = best.get(key)
        if existing is None:
            best[key] = merged
            continue
        if score > float(existing.get("score") or 0.0):
            matched = _merge_matched_by(existing, merged)
            if matched:
                merged["matched_by"] = matched
            best[key] = merged

    return sorted(best.values(), key=lambda row: float(row.get("score") or 0.0), reverse=True)


def expand_candidates_to_parent_paragraphs(db_path: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Load paragraph bodies from corpus.db and merge candidate sub-chunks."""
    if not candidates:
        return []
    locators = {_locator_from_candidate(c) for c in candidates}
    conn = connect_corpus_db(str(db_path))
    try:
        bodies = paragraph_bodies_for_locators(conn, locators)
    finally:
        conn.close()
    return merge_candidates_by_paragraph(candidates, bodies)


def merge_nodes_by_paragraph(nodes: list[Any], paragraph_bodies: dict[tuple[str, int], str]) -> list[Any]:
    """Collapse LlamaIndex nodes to one per paragraph; expand node text to full paragraph."""
    if not nodes:
        return []

    best: dict[tuple[str, int], Any] = {}
    for node in nodes:
        meta = dict(node.node.metadata or {})
        key = (str(meta.get("doc_url") or ""), int(meta.get("para_index") or 0))
        body = paragraph_bodies.get(key)
        if body:
            node.node.text = body
            meta["parent_expanded"] = True
            node.node.metadata = meta
        score = float(node.score or 0.0)
        existing = best.get(key)
        if existing is None:
            best[key] = node
            continue
        if score > float(existing.score or 0.0):
            left_meta = dict(existing.node.metadata or {})
            right_meta = dict(meta)
            left_matched = left_meta.get("matched_by")
            right_matched = right_meta.get("matched_by")
            if left_matched or right_matched:
                merged_matched: set[str] = set()
                for value in (left_matched, right_matched):
                    if isinstance(value, (list, tuple, set)):
                        merged_matched.update(str(x) for x in value)
                    elif value:
                        merged_matched.add(str(value))
                if merged_matched:
                    right_meta["matched_by"] = sorted(merged_matched)
                    node.node.metadata = right_meta
            best[key] = node

    return sorted(best.values(), key=lambda n: float(n.score or 0.0), reverse=True)


def expand_nodes_to_parent_paragraphs(db_path: str, nodes: list[Any]) -> list[Any]:
    """Load paragraph bodies from corpus.db and merge LlamaIndex sub-chunk nodes."""
    if not nodes:
        return []
    locators: set[tuple[str, int]] = set()
    for node in nodes:
        meta = node.node.metadata or {}
        locators.add((str(meta.get("doc_url") or ""), int(meta.get("para_index") or 0)))
    conn = connect_corpus_db(str(db_path))
    try:
        bodies = paragraph_bodies_for_locators(conn, locators)
    finally:
        conn.close()
    return merge_nodes_by_paragraph(nodes, bodies)


__all__ = [
    "expand_candidates_to_parent_paragraphs",
    "expand_nodes_to_parent_paragraphs",
    "merge_candidates_by_paragraph",
    "merge_nodes_by_paragraph",
]
