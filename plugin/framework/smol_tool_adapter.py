# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared adaptation from ToolBase JSON Schema to smolagents ``Tool`` (librarian + specialized)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from plugin.contrib.smolagents.tools import Tool as SmolTool

if TYPE_CHECKING:
    from plugin.framework.tool_base import ToolBase
    from plugin.framework.tool_context import ToolContext

# Match ``writeragent.specialized`` messages when ``safe=True`` (delegation path).
_spec_log = logging.getLogger("writeragent.specialized")

SmolInputsStyle = Literal["librarian", "specialized"]


def to_smol_inputs(parameters: dict[str, Any] | None, *, style: SmolInputsStyle = "librarian") -> dict[str, dict[str, Any]]:
    """Convert ToolBase ``parameters`` (JSON Schema) to smolagents ``inputs`` dict.

    * **librarian** — minimal keys, ``nullable`` from ``required`` (legacy librarian onboarding).
    * **specialized** — merge each property schema so ``enum`` and extra keys are preserved;
      default missing ``type`` to ``\"any\"`` (legacy specialized delegation).
    """
    schema = parameters or {}
    props = schema.get("properties") or {}
    if style == "librarian":
        required = set(schema.get("required") or [])
        out: dict[str, dict[str, Any]] = {}
        for p_name, p_schema in props.items():
            out[p_name] = {"type": p_schema.get("type", "string"), "description": p_schema.get("description", ""), "nullable": p_name not in required}
        return out

    out_sp: dict[str, dict[str, Any]] = {}
    for param_name, spec in props.items():
        merged = dict(spec)
        merged["type"] = spec.get("type", "any")
        merged["description"] = spec.get("description", "")
        out_sp[param_name] = merged
    return out_sp


class SmolToolAdapter(SmolTool):
    """Wraps a ``ToolBase`` for smolagents with configurable execution semantics."""

    skip_forward_signature_validation = True

    def __init__(self, tool: ToolBase, tctx: ToolContext, *, safe: bool = False, main_thread_sync: bool = False, inputs_style: SmolInputsStyle = "librarian", output_type: str | None = None) -> None:
        self._inner_tool = tool
        self._inner_tctx = tctx
        self._safe = safe
        self._main_thread_sync = main_thread_sync
        self.name = cast("str", tool.name or "")
        self.description = tool.description
        params = getattr(tool, "parameters", None) or {}
        self.inputs = to_smol_inputs(params, style=inputs_style)
        if output_type is not None:
            self.output_type = output_type
        elif inputs_style == "librarian":
            self.output_type = "any"
        else:
            self.output_type = "object"
        super().__init__()

    def __call__(self, *args: Any, sanitize_inputs_outputs: bool = False, **kwargs: Any) -> Any:
        return super().__call__(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

    def forward(self, **kwargs: Any) -> Any:
        tool = self._inner_tool
        ctx = self._inner_tctx
        if not self._safe:
            return tool.execute(ctx, **kwargs)
        if getattr(tool, "is_async", lambda: False)():
            _spec_log.debug("Specialized agent executing async tool '%s' on worker", self.name)
            return tool.execute_safe(ctx, **kwargs)
        if self._main_thread_sync:
            from plugin.framework.queue_executor import execute_on_main_thread

            _spec_log.debug("Specialized agent executing sync tool '%s' on main thread", self.name)
            return execute_on_main_thread(tool.execute_safe, ctx, **kwargs)
        return tool.execute_safe(ctx, **kwargs)
