# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared text splitting for embeddings index (sentence chunks for prose; 512/64 for tabular/slides)."""
from __future__ import annotations

from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, str_bounded, deal

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
MIN_CHUNK = 120
DEFAULT_SENTENCE_LOCALE = "en@ss=standard"

# Regex splitter already off; floor spans/passage (33211730747 still ~35m at 4).
_DEAL_SENT_LIST_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SHAPE_DIM
_DEAL_SENT_SPAN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_SENT_TEXT_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_PASSAGE_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE


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


@deal.pre(
    lambda text, locale=DEFAULT_SENTENCE_LOCALE, *_unused, **__: str_bounded(text, _DEAL_PASSAGE_LEN)
    and (locale == DEFAULT_SENTENCE_LOCALE or ascii_bounded(locale, DEAL_MAX_TOKEN))
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


@deal.pre(
    lambda passage, spans, base_meta, *_unused, **__: str_bounded(passage, _DEAL_PASSAGE_LEN)
    and isinstance(spans, list)
    and len(spans) <= _DEAL_SENT_LIST_LEN
)
@deal.post(lambda result: isinstance(result, list))
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


@deal.pre(
    lambda sentences: type(sentences) is list
    and len(sentences) <= _DEAL_SENT_LIST_LEN
    and all(
        type(s) is tuple
        and len(s) == 3
        and type(s[0]) is int
        and type(s[1]) is int
        and 0 <= s[0] <= _DEAL_SENT_SPAN
        and 0 <= s[1] <= _DEAL_SENT_SPAN
        and type(s[2]) is str
        and ascii_bounded(s[2], _DEAL_SENT_TEXT_LEN)
        for s in sentences
    )
)
def _sentences_spans_ok(sentences: object) -> bool:
    """True when *sentences* is ordered ``(start, end, text)`` with ``0 <= start <= end``.

    Successive starts must be ``>=`` the previous end (production splitters are sequential).
    """
    # crosshair: off  # deal.pre predicate; covering it is circular (cover-all 33258921875: 267k lines). Doable later.
    if not isinstance(sentences, list) or len(sentences) > DEAL_MAX_SHAPE_DIM:
        return False
    prev_end: int | None = None
    for item in sentences:
        if not (isinstance(item, tuple) and len(item) == 3):
            return False
        start, end, _sent = item
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
            return False
        if prev_end is not None and start < prev_end:
            return False
        prev_end = end
    return True


def _filter_ordered_sentence_spans(
    sentences: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Drop invalid / out-of-order triples (deal_shim is a no-op under LibreOffice)."""
    ordered: list[tuple[int, int, str]] = []
    prev_end: int | None = None
    for start, end, sent in sentences:
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
            continue
        if prev_end is not None and start < prev_end:
            continue
        ordered.append((start, end, sent))
        prev_end = end
    return ordered


@deal.pre(
    lambda passage, sentences, *_unused, **__: str_bounded(passage, _DEAL_PASSAGE_LEN)
    and _sentences_spans_ok(sentences)
)
@deal.post(lambda result: isinstance(result, list) and all(isinstance(s, tuple) and len(s) == 2 and 0 <= s[0] <= s[1] for s in result))
def _merge_small_sentences_to_spans(
    passage: str,
    sentences: list[tuple[int, int, str]],
    *,
    min_chunk: int = MIN_CHUNK,
) -> list[tuple[int, int]]:
    """One chunk per sentence; glue consecutive sub-*min_chunk* sentences within the passage."""
    sentences = _filter_ordered_sentence_spans(sentences)
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
        if fold_remainder and buffer_len() < min_chunk and spans and buffer_end >= spans[-1][0]:
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


@deal.pre(lambda passage: ascii_bounded(passage, _DEAL_PASSAGE_LEN))
def _split_passage_whitespace_to_sentences(passage: str) -> list[tuple[int, int, str]]:
    # Regex splitter is engine-hostile under CrossHair (cover-all 33180040863 ~24m).
    # crosshair: off
    from plugin.writer.locale.grammar_proofread_locale import GRAMMAR_WHITESPACE_RUN_RE, split_sentence_chunks_by_separator_regex

    sentences: list[tuple[int, int, str]] = []
    for start, chunk in split_sentence_chunks_by_separator_regex(passage, GRAMMAR_WHITESPACE_RUN_RE):
        end = start + len(chunk)
        sentences.append((start, end, chunk))
    return sentences or [(0, len(passage), passage)]


@deal.pre(
    lambda passage, locale_bcp47=None, *_unused, **__: str_bounded(passage, _DEAL_PASSAGE_LEN)
    and (locale_bcp47 is None or ascii_bounded(locale_bcp47, DEAL_MAX_TOKEN))
)
def _split_prose_passage_to_spans(passage: str, locale_bcp47: str | None = None) -> list[tuple[int, int]]:
    # crosshair: off  # ICU/locale sentence merge still combinatoric (cover-all 33569420452: ~738s est / 2036 ex despite str_bounded). Doable later.
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


@deal.pre(lambda passage, *_unused, **__: str_bounded(passage, _DEAL_PASSAGE_LEN))
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


@deal.pre(
    lambda text, runs, base_meta, *_unused, **__: str_bounded(text, _DEAL_PASSAGE_LEN)
    and isinstance(runs, list)
    and len(runs) <= _DEAL_SENT_LIST_LEN
    and isinstance(base_meta, dict)
    and len(base_meta) <= _DEAL_SENT_LIST_LEN
)
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


@deal.pre(
    lambda text, base_meta, *args, **kwargs: ascii_bounded(text, _DEAL_PASSAGE_LEN)
    and type(base_meta) is dict
    and len(base_meta) <= _DEAL_SENT_LIST_LEN
    and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN)
        and (v is None or type(v) in (int, float, bool) or (isinstance(v, str) and ascii_bounded(v, DEAL_MAX_TOKEN)))
        for k, v in base_meta.items()
    )
    and (kwargs.get("locale_bcp47") is None or ascii_bounded(kwargs.get("locale_bcp47"), DEAL_MAX_TOKEN))
)
def split_passage_to_chunk_meta(
    text: str,
    base_meta: dict[str, Any],
    *,
    prose: bool = True,
    locale_bcp47: str | None = None,
) -> list[dict[str, Any]]:
    # crosshair: off  # LocaleTextRun + prose/non-prose split (cover-all 33569420452: ~958s est / 2642 ex despite ascii_bounded meta). Doable later.
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
