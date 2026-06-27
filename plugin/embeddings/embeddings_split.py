# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared text splitting for embeddings index (sentence chunks for prose; 512/64 for tabular/slides)."""
from __future__ import annotations

from typing import Any

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
MIN_CHUNK = 120
DEFAULT_SENTENCE_LOCALE = "en@ss=standard"


def _embeddings_pip_install_hint() -> str:
    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    return EMBEDDINGS_VENV_PIP_INSTALL


def _import_splitter() -> Any:
    import importlib

    try:
        mod = importlib.import_module("langchain_text_splitters")
    except ImportError as exc:
        raise ImportError(
            "langchain-text-splitters is not installed in the configured Python venv. "
            f"Install with: {_embeddings_pip_install_hint()}"
        ) from exc
    return mod.RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", " ", ""],
    )


def split_passage_to_sentences(text: str, locale: str = DEFAULT_SENTENCE_LOCALE) -> list[tuple[int, int, str]]:
    """Split *text* into ``(char_start, char_end, sentence)`` relative to *text*."""
    passage = str(text or "")
    if not passage.strip():
        return []

    try:
        from icu4py.breakers import SentenceBreaker
    except ImportError as exc:
        raise ImportError(
            "icu4py is not installed in the configured Python venv. "
            f"Install with: {_embeddings_pip_install_hint()}"
        ) from exc

    sentences: list[tuple[int, int, str]] = []
    search_from = 0
    for piece in SentenceBreaker(passage, locale):
        sent = str(piece)
        if not sent:
            continue
        start = passage.find(sent, search_from)
        if start < 0:
            start = search_from
        end = start + len(sent)
        sentences.append((start, end, sent))
        search_from = end
    return sentences or [(0, len(passage), passage)]


def _meta_chunks_from_spans(
    passage: str,
    spans: list[tuple[int, int]],
    base_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for char_start, char_end in spans:
        piece = passage[char_start:char_end]
        if not piece.strip():
            continue
        meta = dict(base_meta)
        meta.update({"char_start": char_start, "char_end": char_end, "text": piece})
        chunks.append(meta)
    return chunks


def _merge_small_sentences_to_spans(
    passage: str,
    sentences: list[tuple[int, int, str]],
    *,
    min_chunk: int = MIN_CHUNK,
) -> list[tuple[int, int]]:
    """One chunk per sentence; glue consecutive sub-*min_chunk* sentences within the passage."""
    if not sentences:
        return []

    spans: list[tuple[int, int]] = []
    buffer_start: int | None = None
    buffer_end: int | None = None

    def buffer_len() -> int:
        if buffer_start is None or buffer_end is None:
            return 0
        return buffer_end - buffer_start

    def flush_buffer(*, fold_remainder: bool) -> None:
        nonlocal buffer_start, buffer_end
        if buffer_start is None or buffer_end is None:
            return
        if fold_remainder and buffer_len() < min_chunk and spans:
            prev_start, _prev_end = spans[-1]
            spans[-1] = (prev_start, buffer_end)
        else:
            spans.append((buffer_start, buffer_end))
        buffer_start = None
        buffer_end = None

    for start, end, sent in sentences:
        sent_len = len(sent)
        if buffer_start is None:
            if sent_len >= min_chunk:
                spans.append((start, end))
                continue
            buffer_start = start
            buffer_end = end
            continue

        if sent_len >= min_chunk:
            flush_buffer(fold_remainder=True)
            spans.append((start, end))
            continue

        buffer_end = end
        if buffer_len() >= min_chunk:
            flush_buffer(fold_remainder=False)

    if buffer_start is not None and buffer_end is not None:
        flush_buffer(fold_remainder=True)

    return spans


def _split_passage_whitespace_to_sentences(passage: str) -> list[tuple[int, int, str]]:
    from plugin.writer.locale.grammar_proofread_locale import GRAMMAR_WHITESPACE_RUN_RE, split_sentence_chunks_by_separator_regex

    sentences: list[tuple[int, int, str]] = []
    for start, chunk in split_sentence_chunks_by_separator_regex(passage, GRAMMAR_WHITESPACE_RUN_RE):
        end = start + len(chunk)
        sentences.append((start, end, chunk))
    return sentences or [(0, len(passage), passage)]


def _split_prose_passage_to_spans(passage: str, locale_bcp47: str | None = None) -> list[tuple[int, int]]:
    from plugin.writer.locale.grammar_proofread_locale import (
        bcp47_to_icu_sentence_breaker_locale,
        is_whitespace_sentence_locale,
        normalize_detected_bcp47,
    )

    canon = normalize_detected_bcp47(locale_bcp47) if locale_bcp47 else None
    if canon and is_whitespace_sentence_locale(canon):
        sentences = _split_passage_whitespace_to_sentences(passage)
    elif canon:
        sentences = split_passage_to_sentences(passage, bcp47_to_icu_sentence_breaker_locale(canon))
    else:
        sentences = split_passage_to_sentences(passage)
    if not sentences:
        return []
    if len(sentences) == 1:
        start, end, _sent = sentences[0]
        return [(start, end)]
    return _merge_small_sentences_to_spans(passage, sentences)


def _split_non_prose_passage_to_spans(passage: str) -> list[tuple[int, int]]:
    if len(passage) <= CHUNK_SIZE:
        return [(0, len(passage))]

    splitter = _import_splitter()
    pieces = splitter.split_text(passage)
    if not pieces:
        return []

    spans: list[tuple[int, int]] = []
    search_from = 0
    for piece in pieces:
        idx = passage.find(piece, search_from)
        if idx < 0:
            idx = search_from
        char_start = idx
        char_end = idx + len(piece)
        spans.append((char_start, char_end))
        search_from = max(0, char_end - CHUNK_OVERLAP)
    return spans


def split_passage_locale_runs_to_chunk_meta(
    text: str,
    runs: list[Any],
    base_meta: dict[str, Any],
    *,
    prose: bool = True,
    doc_default_locale: str | None = None,
) -> list[dict[str, Any]]:
    """Split one passage using per-run locales; MIN_CHUNK glue never crosses locale boundaries."""
    from plugin.embeddings.embeddings_fs import LocaleTextRun

    passage = str(text or "")
    if not passage.strip() or not runs:
        return []

    if not prose:
        return split_passage_to_chunk_meta(passage, base_meta, prose=False)

    all_spans: list[tuple[int, int]] = []
    for run in runs:
        if not isinstance(run, LocaleTextRun):
            continue
        run_text = passage[run.char_start : run.char_end]
        if not run_text.strip():
            continue
        locale = run.locale_bcp47 if run.locale_bcp47 is not None else doc_default_locale
        run_spans = _split_prose_passage_to_spans(run_text, locale)
        for start, end in run_spans:
            all_spans.append((run.char_start + start, run.char_start + end))

    if not all_spans:
        return []

    all_spans.sort(key=lambda item: (item[0], item[1]))
    return _meta_chunks_from_spans(passage, all_spans, base_meta)


def split_passage_to_chunk_meta(
    text: str,
    base_meta: dict[str, Any],
    *,
    prose: bool = True,
    locale_bcp47: str | None = None,
) -> list[dict[str, Any]]:
    """Split one passage into embed-sized chunks with char offsets relative to passage text."""
    from plugin.embeddings.embeddings_fs import LocaleTextRun

    stripped = str(text or "").strip()
    if not stripped:
        return []

    if prose:
        runs = [LocaleTextRun(char_start=0, char_end=len(stripped), locale_bcp47=locale_bcp47)]
        return split_passage_locale_runs_to_chunk_meta(
            stripped,
            runs,
            base_meta,
            prose=True,
            doc_default_locale=locale_bcp47,
        )

    spans = _split_non_prose_passage_to_spans(stripped)
    return _meta_chunks_from_spans(stripped, spans, base_meta)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "DEFAULT_SENTENCE_LOCALE",
    "MIN_CHUNK",
    "split_passage_locale_runs_to_chunk_meta",
    "split_passage_to_chunk_meta",
    "split_passage_to_sentences",
]
