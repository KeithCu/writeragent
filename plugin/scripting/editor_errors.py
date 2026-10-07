# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Editor failure detail and message formatting for user-visible dialogs."""

from __future__ import annotations

import traceback
from typing import TypeVar

from plugin.framework.deal_shim import (
    DEAL_MAX_SOURCE,
    UNDER_CROSSHAIR,
    deal,
    str_bounded,
)

T = TypeVar("T")


def _profile(pytest_val: T, crosshair_val: T) -> T:
    return crosshair_val if UNDER_CROSSHAIR else pytest_val


def _deal_exc_ok_pytest(exc: object) -> bool:
    return isinstance(exc, BaseException)


def _deal_exc_ok_crosshair(exc: object) -> bool:
    return exc is None


_deal_exc_ok = _profile(_deal_exc_ok_pytest, _deal_exc_ok_crosshair)


def _deal_optional_exc_ok_pytest(exc: object) -> bool:
    return exc is None or isinstance(exc, BaseException)


def _deal_optional_exc_ok_crosshair(exc: object) -> bool:
    return exc is None


_deal_optional_exc_ok = _profile(_deal_optional_exc_ok_pytest, _deal_optional_exc_ok_crosshair)


def _deal_failure_detail_ok_pytest(detail: object = None, exc: object = None) -> bool:
    return (detail is None or isinstance(detail, str)) and _deal_optional_exc_ok(exc)


def _deal_failure_detail_ok_crosshair(detail: object = None, exc: object = None) -> bool:
    return (detail is None or str_bounded(detail, DEAL_MAX_SOURCE)) and exc is None


_deal_failure_detail_ok = _profile(_deal_failure_detail_ok_pytest, _deal_failure_detail_ok_crosshair)


def _deal_failure_message_ok(summary: object, detail: object = None, exc: object = None) -> bool:
    if not isinstance(summary, str):
        return False
    if UNDER_CROSSHAIR and not str_bounded(summary, DEAL_MAX_SOURCE):
        return False
    return _deal_failure_detail_ok(detail, exc)


@deal.pre(lambda exc: _deal_exc_ok(exc))
def exception_traceback(exc: BaseException) -> str:
    """Full traceback string for *exc*."""
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
