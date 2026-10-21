# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Editor failure detail and message formatting for user-visible dialogs."""

from __future__ import annotations

import traceback

from plugin.framework.deal_shim import (
    DEAL_MAX_SOURCE,
    _profile,
    deal,
    str_bounded,
)


def _deal_text_ok_pytest(value: object) -> bool:
    return isinstance(value, str)


def _deal_text_ok_crosshair(value: object) -> bool:
    return str_bounded(value, DEAL_MAX_SOURCE)


_deal_text_ok = _profile(_deal_text_ok_crosshair, _deal_text_ok_pytest)


def _deal_optional_exc_ok_pytest(exc: object) -> bool:
    return exc is None or isinstance(exc, BaseException)


def _deal_optional_exc_ok_crosshair(exc: object) -> bool:
    return exc is None


_deal_optional_exc_ok = _profile(_deal_optional_exc_ok_crosshair, _deal_optional_exc_ok_pytest)


def _deal_failure_detail_ok(detail: object = None, exc: object = None) -> bool:
    return (detail is None or _deal_text_ok(detail)) and _deal_optional_exc_ok(exc)


def _deal_failure_message_ok_pytest(summary: object, detail: object = None, exc: object = None) -> bool:
    return _deal_text_ok_pytest(summary) and _deal_failure_detail_ok(detail, exc)


def _deal_failure_message_ok_crosshair(summary: object, detail: object = None, exc: object = None) -> bool:
    return _deal_text_ok_crosshair(summary) and _deal_failure_detail_ok(detail, exc)


_deal_failure_message_ok = _profile(_deal_failure_message_ok_crosshair, _deal_failure_message_ok_pytest)


@deal.pre(lambda exc: isinstance(exc, BaseException))
def exception_traceback(exc: BaseException) -> str:
    """Full traceback for *exc*.

    The pre is isinstance(exc, BaseException) on both profiles because a
    CrossHair domain of exc is None has no __traceback__; the callable
    stays # crosshair: off so traceback.format_exception is not a
    check/cover entry.
    """
    # crosshair: off
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@deal.pre(lambda detail=None, exc=None: _deal_failure_detail_ok(detail, exc))
@deal.post(lambda result: isinstance(result, str))
def failure_detail(*, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Combine subprocess stderr, probe output, and/or an exception traceback."""
    # crosshair: off
    chunks: list[str] = []
    detail_text = (detail or "").strip()
    if detail_text:
        chunks.append(detail_text)
    if exc is not None:
        chunks.append(exception_traceback(exc).rstrip())
    return "\n\n".join(chunks)


@deal.pre(lambda summary, detail=None, exc=None: _deal_failure_message_ok(summary, detail, exc))
@deal.post(lambda result: isinstance(result, str))
def failure_message(summary: str, *, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Build a msgbox body: *summary* plus optional detail/traceback blocks."""
    # crosshair: off
    body = failure_detail(detail=detail, exc=exc)
    if body:
        return f"{summary}\n\n{body}"
    return summary
