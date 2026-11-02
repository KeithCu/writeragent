# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.editor_errors."""

from __future__ import annotations

import deal

from plugin.scripting import editor_errors as _prod
from tests.contracts.bounds import DEAL_MAX_SOURCE, profile, str_bounded

_deal_text_ok = profile(
    lambda text: str_bounded(text, DEAL_MAX_SOURCE),
    lambda text: isinstance(text, str),
)

_deal_optional_exc_ok = profile(
    lambda exc: exc is None,
    lambda exc: exc is None or isinstance(exc, BaseException),
)

exception_traceback = deal.pre(lambda exc: isinstance(exc, BaseException))(
    _prod.exception_traceback
)

failure_detail = deal.pre(
    lambda detail=None, exc=None: (detail is None or _deal_text_ok(detail))
    and _deal_optional_exc_ok(exc)
)(_prod.failure_detail)

failure_message = deal.pre(
    lambda summary, detail=None, exc=None: _deal_text_ok(summary)
    and (detail is None or _deal_text_ok(detail))
    and _deal_optional_exc_ok(exc)
)(_prod.failure_message)

PYTEST = ["exception_traceback", "failure_detail", "failure_message"]
TARGETS: list[str] = []
