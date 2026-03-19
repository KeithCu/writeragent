# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2025-2026 quazardous (config, registries, build system)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Central tool registry with unified execution."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.framework.schema_convert import to_openai_schema, to_mcp_schema
from plugin.framework.errors import ToolExecutionError

log = logging.getLogger("writeragent.tools")


class ToolRegistry:
    """Registers and dispatches tools.

    Both the chatbot and MCP server use this single registry.
    """

    def __init__(self, services):
        self._services = services
        self._tools = {}  # name -> ToolBase instance
        self.batch_mode = False  # suppress per-tool cache invalidation

    # ── Registration ──────────────────────────────────────────────────

    def register(self, tool):
        """Register a single ToolBase instance."""
        if tool.name in self._tools:
            # If it's the exact same class, skip silently.
            if type(self._tools[tool.name]) is type(tool):
                return
            log.warning("Tool already registered, replacing: %s", tool.name)
        self._tools[tool.name] = tool

    def register_many(self, tools):
        for t in tools:
            self.register(t)

    def auto_discover(self, module):
        """Automatically discover and register ToolBase subclasses in a module."""
        import inspect
        from plugin.framework.tool_base import ToolBase, ToolBaseDummy

        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Must inherit from ToolBase, but not be ToolBase itself or ToolBaseDummy
            # Must be defined in this module to avoid double registration from imports
            # Also exclude abstract classes or classes without a defined 'name'
            if (issubclass(obj, ToolBase) and
                obj is not ToolBase and
                not issubclass(obj, ToolBaseDummy) and
                obj.__module__ == module.__name__ and
                not inspect.isabstract(obj) and
                getattr(obj, "name", None)):

                try:
                    tool_instance = obj()
                    self.register(tool_instance)
                except Exception as e:
                    log.error("Failed to instantiate tool %s: %s", obj.__name__, e)

    # ── Lookup & Schema Generation ────────────────────────────────────

    def get_tools(self, doc_type=None, tier=None, intent=None, names=None, filter_doc_type=True):
        """Return a list of ToolBase instances matching the given criteria.

        Args:
            doc_type: Optional string indicating compatibility. If None, only universal tools are returned (unless filter_doc_type=False).
            tier: Optional string (e.g. "core", "extended").
            intent: Optional string filtering by tool intent.
            names: Optional list of specific tool names to include.
            filter_doc_type: If True, filters by doc_type. Defaults to True.
        """
        tools = self._tools.values()
        if filter_doc_type:
            tools = [t for t in tools if t.doc_types is None or (doc_type is not None and doc_type in t.doc_types)]
        if tier:
            tools = [t for t in tools if t.tier == tier]
        if intent:
            tools = [t for t in tools if t.intent == intent]
        if names:
            tools = [t for t in tools if t.name in names]
        return list(tools)

    def get_schemas(self, protocol="openai", **kwargs):
        """Return schemas for tools matching the given kwargs criteria.

        Args:
            protocol: Either "openai" or "mcp".
            **kwargs: Filters passed to get_tools().
        """
        tools = self.get_tools(**kwargs)
        if protocol == "openai":
            return [to_openai_schema(t) for t in tools]
        elif protocol == "mcp":
            return [to_mcp_schema(t) for t in tools]
        else:
            raise ValueError(f"Unknown protocol: {protocol}")

    def get_tool_summaries(self, **kwargs):
        """Lightweight catalogue: ``[{"name", "description", "tier", "intent"}]``."""
        tools = self.get_tools(**kwargs)
        return [{"name": t.name,
                 "description": (t.description or "")[:120],
                 "tier": t.tier,
                 "intent": t.intent}
                for t in tools]

    def get(self, name):
        """Get a tool by name, or None."""
        return self._tools.get(name)

    # ── Execution ─────────────────────────────────────────────────────

    def execute(self, tool_name, ctx, **kwargs):
        """Execute a tool by name.

        Args:
            tool_name: Registered tool name.
            ctx:       ToolContext for this invocation.
            **kwargs:  Tool arguments.

        Returns:
            dict result from the tool.

        Raises:
            KeyError:     Tool not found.
            ValueError:   Validation failed or doc_type incompatible.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(f"Unknown tool: {tool_name}")

        # Check doc_type compatibility
        if tool.doc_types and ctx.doc_type and ctx.doc_type not in tool.doc_types:
            raise ValueError(
                f"Tool {tool_name} does not support doc_type={ctx.doc_type}"
            )

        # Restrict kwargs to this tool's schema so extra keys (e.g. image_model
        # from API/LLM) do not cause "Unknown parameter" validation errors.
        props = (tool.parameters or {}).get("properties", {})
        if props:
            kwargs = {k: v for k, v in kwargs.items() if k in props}

        from plugin.framework.errors import format_error_payload, WriterAgentException

        # Common context for all error details
        common_details = {"tool_name": tool_name}
        if ctx.caller:
            common_details["caller"] = ctx.caller
        if ctx.doc_type:
            common_details["doc_type"] = ctx.doc_type

        # Validate parameters
        ok, err = tool.validate(**kwargs)
        if not ok:
            return {
                "status": "error",
                "code": "VALIDATION_ERROR",
                "message": err,
                "details": common_details
            }

        # Emit executing event
        bus = self._services.get("events")
        if bus:
            bus.emit("tool:executing", name=tool_name, caller=ctx.caller)

        # Invalidate document cache on mutations (skipped in batch mode)
        if tool.detects_mutation() and not self.batch_mode:
            doc_svc = self._services.get("document")
            if doc_svc:
                doc_svc.invalidate_cache(ctx.doc)

        try:
            result = tool.execute(ctx, **kwargs)
            # Ensure any returned dict with status='error' includes full context details
            if isinstance(result, dict) and result.get("status") == "error":
                result_details = result.get("details", {})
                if isinstance(result_details, dict):
                    # merge common_details into result_details without overwriting existing keys
                    for k, v in common_details.items():
                        if k not in result_details:
                            result_details[k] = v
                    result["details"] = result_details
        except ToolExecutionError as exc:
            log.exception("Tool execution failed (ToolExecutionError): %s", tool_name)
            if bus:
                bus.emit("tool:failed", name=tool_name, error=str(exc), caller=ctx.caller)

            error_details = exc.details or {}
            for k, v in common_details.items():
                if k not in error_details:
                    error_details[k] = v
            return {"status": "error", "code": exc.code, "message": exc.message, "details": error_details}
        except Exception as exc:
            log.exception("Tool execution failed: %s", tool_name)
            if bus:
                bus.emit("tool:failed", name=tool_name, error=str(exc), caller=ctx.caller)
            payload = format_error_payload(exc)
            error_details = payload.get("details", {})
            for k, v in common_details.items():
                if k not in error_details:
                    error_details[k] = v
            payload["details"] = error_details
            return payload

        if bus:
            bus.emit("tool:completed", name=tool_name, caller=ctx.caller)

        return result

    @property
    def tool_names(self):
        return list(self._tools.keys())

    def __len__(self):
        return len(self._tools)
