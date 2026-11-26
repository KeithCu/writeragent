# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run registered tools against native Impress and Draw documents in UNO tests."""

from __future__ import annotations

import json
from typing import Any

from plugin.tests.testing_utils import TestingFactory


def exec_tool(doc: Any, ctx: Any, name: str, args: dict[str, Any], *, doc_type: str = "impress") -> Any:
    """Run tool *name* with *args* and return its result dict."""
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type=doc_type)


def exec_tool_json(doc: Any, ctx: Any, name: str, args: dict[str, Any], *, doc_type: str = "impress") -> str:
    """Run tool *name* and return its result as JSON text, for tests that parse or print it."""
    return json.dumps(exec_tool(doc, ctx, name, args, doc_type=doc_type))
