"""Contracts sidecar for plugin.embeddings.embeddings_split."""

from __future__ import annotations

import deal

from plugin.embeddings import embeddings_split as _prod
from plugin.embeddings.embeddings_split import DEFAULT_SENTENCE_LOCALE
from tests.contracts.bounds import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    str_bounded,
)

_DEAL_SENT_LIST_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SHAPE_DIM
_DEAL_SENT_SPAN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_SENT_TEXT_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_PASSAGE_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE


def _deal_sentence_locale_ok_pytest(locale: object) -> bool:
    return locale == DEFAULT_SENTENCE_LOCALE or ascii_bounded(locale, DEAL_MAX_TOKEN)


def _deal_sentence_locale_ok_crosshair(locale: object) -> bool:
    return locale is DEFAULT_SENTENCE_LOCALE


def _deal_chunk_base_meta_ok_pytest(base_meta: object) -> bool:
    return type(base_meta) is dict and all(type(k) is str for k in base_meta)


def _deal_chunk_base_meta_ok_crosshair(base_meta: object) -> bool:
    return (
        type(base_meta) is dict
        and len(base_meta) <= _DEAL_SENT_LIST_LEN
        and all(
            type(k) is str
            and ascii_bounded(k, DEAL_MAX_TOKEN)
            and (
                v is None
                or type(v) in (int, float, bool)
                or (isinstance(v, str) and ascii_bounded(v, DEAL_MAX_TOKEN))
            )
            for k, v in base_meta.items()
        )
    )


_deal_chunk_base_meta_ok = (
    _deal_chunk_base_meta_ok_crosshair if UNDER_CROSSHAIR else _deal_chunk_base_meta_ok_pytest
)


def _deal_run_locale_ok_pytest(locale: object) -> bool:
    return locale is None or ascii_bounded(locale, DEAL_MAX_TOKEN)


def _deal_run_locale_ok_crosshair(locale: object) -> bool:
    return locale is None


_deal_run_locale_ok = _deal_run_locale_ok_crosshair if UNDER_CROSSHAIR else _deal_run_locale_ok_pytest


def _deal_locale_run_ok(run: object) -> bool:
    return _deal_run_locale_ok(getattr(run, "locale_bcp47", None))


_deal_sentence_locale_ok = (
    _deal_sentence_locale_ok_crosshair if UNDER_CROSSHAIR else _deal_sentence_locale_ok_pytest
)


def _deal_passage_text_ok_pytest(text: object) -> bool:
    return isinstance(text, str)


def _deal_passage_text_ok_crosshair(text: object) -> bool:
    return (
        isinstance(text, str)
        and len(text) <= _DEAL_PASSAGE_LEN
        and text.isascii()
        and all(c.isprintable() or c in "\n\t\r" for c in text)
    )


_deal_passage_text_ok = (
    _deal_passage_text_ok_crosshair if UNDER_CROSSHAIR else _deal_passage_text_ok_pytest
)


def _deal_passage_str_ok_pytest(passage: object) -> bool:
    return isinstance(passage, str)


def _deal_passage_str_ok_crosshair(passage: object) -> bool:
    return str_bounded(passage, _DEAL_PASSAGE_LEN)


_deal_passage_str_ok = _deal_passage_str_ok_crosshair if UNDER_CROSSHAIR else _deal_passage_str_ok_pytest


def _deal_ws_passage_ok_pytest(passage: object) -> bool:
    return isinstance(passage, str)


def _deal_ws_passage_ok_crosshair(passage: object) -> bool:
    return ascii_bounded(passage, _DEAL_PASSAGE_LEN)


_deal_ws_passage_ok = _deal_ws_passage_ok_crosshair if UNDER_CROSSHAIR else _deal_ws_passage_ok_pytest


def _deal_span_list_ok_pytest(spans: object) -> bool:
    return isinstance(spans, list)


def _deal_span_list_ok_crosshair(spans: object) -> bool:
    return isinstance(spans, list) and len(spans) <= _DEAL_SENT_LIST_LEN


_deal_span_list_ok = _deal_span_list_ok_crosshair if UNDER_CROSSHAIR else _deal_span_list_ok_pytest


def _deal_sentences_spans_pre_pytest(sentences: object) -> bool:
    return True


def _deal_sentences_spans_pre_crosshair(sentences: object) -> bool:
    return (
        type(sentences) is list
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


_deal_sentences_spans_pre = (
    _deal_sentences_spans_pre_crosshair if UNDER_CROSSHAIR else _deal_sentences_spans_pre_pytest
)


def _sentences_spans_shape_ok_pytest(sentences: object) -> bool:
    return isinstance(sentences, list)


def _sentences_spans_shape_ok_crosshair(sentences: object) -> bool:
    return isinstance(sentences, list) and len(sentences) <= _DEAL_SENT_LIST_LEN


_sentences_spans_shape_ok = (
    _sentences_spans_shape_ok_crosshair if UNDER_CROSSHAIR else _sentences_spans_shape_ok_pytest
)


@deal.pre(lambda sentences: _deal_sentences_spans_pre(sentences))
def _sentences_spans_ok(sentences: object) -> bool:
    if not isinstance(sentences, list) or not _sentences_spans_shape_ok(sentences):
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


split_passage_to_sentences = deal.pre(
    lambda text, locale=DEFAULT_SENTENCE_LOCALE, *_unused, **__: _deal_passage_text_ok(text)
    and _deal_sentence_locale_ok(locale)
)(_prod.split_passage_to_sentences)

_meta_chunks_from_spans = deal.pre(
    lambda passage, spans, base_meta, *_unused, **__: _deal_passage_str_ok(passage)
    and _deal_span_list_ok(spans)
    and _deal_chunk_base_meta_ok(base_meta)
)(
    deal.post(lambda result: isinstance(result, list))(_prod._meta_chunks_from_spans)
)

_merge_small_sentences_to_spans = deal.pre(
    lambda passage, sentences, *_unused, **__: _deal_passage_str_ok(passage)
    and _sentences_spans_ok(sentences)
)(
    deal.post(
        lambda result: isinstance(result, list)
        and all(isinstance(s, tuple) and len(s) == 2 and 0 <= s[0] <= s[1] for s in result)
    )(_prod._merge_small_sentences_to_spans)
)

_split_passage_whitespace_to_sentences = deal.pre(lambda passage: _deal_ws_passage_ok(passage))(
    _prod._split_passage_whitespace_to_sentences
)

_split_prose_passage_to_spans = deal.pre(
    lambda passage, locale_bcp47=None, *_unused, **__: _deal_passage_str_ok(passage)
    and _deal_run_locale_ok(locale_bcp47)
)(_prod._split_prose_passage_to_spans)

_split_non_prose_passage_to_spans = deal.pre(
    lambda passage, *_unused, **__: _deal_passage_str_ok(passage)
)(_prod._split_non_prose_passage_to_spans)

split_passage_locale_runs_to_chunk_meta = deal.pre(
    lambda text, runs, base_meta, *args, **kwargs: _deal_passage_text_ok(text)
    and _deal_span_list_ok(runs)
    and _deal_chunk_base_meta_ok(base_meta)
    and all(_deal_locale_run_ok(r) for r in runs)
    and _deal_run_locale_ok(kwargs.get("doc_default_locale"))
)(_prod.split_passage_locale_runs_to_chunk_meta)

split_passage_to_chunk_meta = deal.pre(
    lambda text, base_meta, *args, **kwargs: _deal_passage_text_ok(text)
    and _deal_chunk_base_meta_ok(base_meta)
    and _deal_run_locale_ok(kwargs.get("locale_bcp47"))
)(_prod.split_passage_to_chunk_meta)

PYTEST = [
    "split_passage_to_sentences",
    "_meta_chunks_from_spans",
    "_sentences_spans_ok",
    "_merge_small_sentences_to_spans",
    "_split_passage_whitespace_to_sentences",
    "_split_prose_passage_to_spans",
    "_split_non_prose_passage_to_spans",
    "split_passage_locale_runs_to_chunk_meta",
    "split_passage_to_chunk_meta",
]

TARGETS = [
    f"{__name__}.split_passage_to_sentences",
    f"{__name__}._meta_chunks_from_spans",
    f"{__name__}._merge_small_sentences_to_spans",
    f"{__name__}._split_non_prose_passage_to_spans",
    f"{__name__}.split_passage_locale_runs_to_chunk_meta",
]
