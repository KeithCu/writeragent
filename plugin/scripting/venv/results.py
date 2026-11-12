# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Error result dict for venv helpers and the sandbox (a leaf: the compute bundle ships it)."""

from __future__ import annotations

import functools
import traceback
from typing import Any, Callable, ParamSpec

P = ParamSpec("P")


class HelperError(ValueError):
    """A venv helper failure with a result code (INVALID_PARAM, UNKNOWN_COLUMN, PARSE_ERROR, ...).

    Coercers and column resolvers raise it; the helper's ``catch_helper_error`` wrapper
    turns it into ``error_result(code, message, helper=<function name>)``. It is a
    ValueError so callers that already catch ValueError around a coercer
    (trusted_dispatch._sheet_layout_flags, _decimals) keep working.
    """

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def catch_helper_error(func: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
    """Return HelperError as the helper's error dict; every other exception propagates.

    Put it under ``@helper(...)`` so the registry (run_domain) stores the wrapper.
    A plain ValueError (bad header_row from resolve_df, pandas) is not caught: run_domain
    still reports <DOMAIN>_FAILED and a direct =PY call still raises.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return func(*args, **kwargs)
        except HelperError as exc:
            return error_result(exc.code, str(exc), helper=func.__name__)

    return wrapper


def error_result(
    code: str | None = None,
    message: str = "",
    *,
    helper: str | None = None,
    details: dict[str, Any] | None = None,
    stdout: str | None = None,
    with_traceback: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "message": message}
    if code is not None:
        out["code"] = code
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    if stdout is not None:
        out["stdout"] = stdout
    if with_traceback:
        out["traceback"] = traceback.format_exc()
    return out
