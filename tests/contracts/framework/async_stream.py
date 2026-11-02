# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.async_stream."""

from __future__ import annotations


import deal

from plugin.framework import async_stream as _prod
from tests.contracts.bounds import DEAL_MAX_TOKEN, ascii_bounded

_format_agent_tool_stream_line = deal.ensure(
    lambda prefix, data, result=None: result is not None and prefix in result
)(
    deal.post(lambda result: isinstance(result, str) and result.startswith("\n") and result.endswith("\n"))(
        deal.pre(lambda prefix, data: ascii_bounded(prefix, DEAL_MAX_TOKEN, min_len=1))(
            _prod._format_agent_tool_stream_line
        )
    )
)

PYTEST = ["_format_agent_tool_stream_line"]
TARGETS: list[str] = []
