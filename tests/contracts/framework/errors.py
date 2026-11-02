# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.errors."""

from __future__ import annotations

import deal

from plugin.framework import errors as _prod

format_error_payload = deal.ensure(
    lambda e, result: not isinstance(e, _prod.WriterAgentException) or result.get("code") == e.code
)(
    deal.ensure(
        lambda e, result: isinstance(e, _prod.WriterAgentException)
        or (
            result.get("code") == "INTERNAL_ERROR"
            and isinstance(result.get("details"), dict)
            and "type" in result["details"]
        )
    )(
        deal.post(
            lambda result: isinstance(result, dict)
            and result.get("status") == "error"
            and "code" in result
            and "message" in result
        )(_prod.format_error_payload)
    )
)

format_error_message = deal.post(lambda result: isinstance(result, str))(_prod.format_error_message)

make_tool_error = deal.post(
    lambda result: isinstance(result, dict)
    and result.get("status") == "error"
    and "code" in result
    and "message" in result
)(_prod.make_tool_error)

PYTEST = ["format_error_payload", "format_error_message", "make_tool_error"]
TARGETS: list[str] = []
