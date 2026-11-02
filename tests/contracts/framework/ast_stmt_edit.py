# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.ast_stmt_edit."""

from __future__ import annotations

import deal

from plugin.framework import ast_stmt_edit as _prod
from tests.contracts.bounds import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded

is_name_call_expr = deal.post(lambda result: isinstance(result, bool))(
    deal.pre(
        lambda node, names: isinstance(names, frozenset)
        and len(names) <= DEAL_MAX_CMD_ARGS
        and all(ascii_bounded(n, DEAL_MAX_TOKEN) for n in names)
    )(_prod.is_name_call_expr)
)

remove_statements = deal.post(
    lambda result: isinstance(result, tuple)
    and len(result) == 2
    and isinstance(result[0], str)
    and isinstance(result[1], int)
    and result[1] >= 0
)(_prod.remove_statements)

PYTEST = ["is_name_call_expr", "remove_statements"]
TARGETS: list[str] = []
