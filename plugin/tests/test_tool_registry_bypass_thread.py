# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression: DSPy eval (tools_lo) may call tools from the LO worker thread."""

import threading
from unittest.mock import MagicMock

from plugin.framework.tool_context import ToolContext
from plugin.framework.tool_registry import ToolRegistry


def test_execute_bypass_thread_guard_allows_background_thread() -> None:
    calls: list[str] = []

    class DummyTool:
        name = "dummy_sync"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        uno_services = None
        doc_types = None

        def validate(self, **kwargs):
            return True, None

        def execute(self, ctx, **kwargs):
            calls.append("execute")
            return {"status": "ok"}

        def execute_safe(self, ctx, **kwargs):
            calls.append("execute_safe")
            return {"status": "ok"}

    reg = ToolRegistry(MagicMock())
    reg.register(DummyTool())  # type: ignore[arg-type]
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", {}, "test")

    out: dict | None = None

    def bg():
        nonlocal out
        out = reg.execute("dummy_sync", ctx, bypass_thread_guard=True)

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert out == {"status": "ok"}
    assert calls == ["execute"]
