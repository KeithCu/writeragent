import inspect
import json
from plugin.framework.tool_base import ToolBase

from plugin.modules.writer.format_support import FORMAT_TOOLS
from plugin.modules.writer.outline import OUTLINE_TOOLS
from plugin.modules.writer.stats import STATS_TOOLS
from plugin.modules.writer.styles import STYLES_TOOLS
from plugin.modules.writer.comments import COMMENTS_TOOLS
from plugin.modules.writer.content import CONTENT_TOOLS
from plugin.modules.writer.tracking import TRACKING_TOOLS
from plugin.modules.writer.tables import TABLES_TOOLS

WRITER_OPS_TOOLS = OUTLINE_TOOLS + STATS_TOOLS + STYLES_TOOLS + COMMENTS_TOOLS + CONTENT_TOOLS + TRACKING_TOOLS + TABLES_TOOLS
WRITER_TOOLS = list(FORMAT_TOOLS) + WRITER_OPS_TOOLS

_WRITER_SCHEMA_BY_NAME = {}
for item in WRITER_TOOLS:
    func = item.get("function", {})
    name = func.get("name")
    if name:
        _WRITER_SCHEMA_BY_NAME[name] = (
            func.get("description", ""),
            func.get("parameters", {"type": "object", "properties": {}, "required": []}),
        )

_READ_ONLY_PREFIXES = ("get_", "read_", "list_", "find_", "search_", "count_")
_READ_ONLY_NAMES = {"find_text", "web_research"}

def _writer_tool_class(name, dispatch_func):
    """Build a ToolBase subclass that delegates to the legacy TOOL_DISPATCH function."""
    desc, params = _WRITER_SCHEMA_BY_NAME.get(name, ("", {"type": "object", "properties": {}, "required": []}))
    is_mutation = name not in _READ_ONLY_NAMES and not any(name.startswith(p) for p in _READ_ONLY_PREFIXES)

    class _WriterTool(ToolBase):
        def execute(self, ctx, **kwargs):
            status_cb = getattr(ctx, "status_callback", None)
            thinking_cb = getattr(ctx, "append_thinking_callback", None)
            sig = inspect.signature(dispatch_func)
            extra = {}
            if "status_callback" in sig.parameters or "kwargs" in sig.parameters:
                extra["status_callback"] = status_cb
            if "append_thinking_callback" in sig.parameters or "kwargs" in sig.parameters:
                extra["append_thinking_callback"] = thinking_cb
            result_str = dispatch_func(ctx.doc, ctx.ctx, kwargs, **extra)
            try:
                return json.loads(result_str) if isinstance(result_str, str) else result_str
            except (json.JSONDecodeError, TypeError):
                return {"status": "error", "message": "Invalid tool response"}

    _WriterTool.name = name
    _WriterTool.description = desc
    _WriterTool.parameters = params
    _WriterTool.doc_types = ["writer"]
    _WriterTool.tier = "core"
    _WriterTool.is_mutation = is_mutation
    return _WriterTool
