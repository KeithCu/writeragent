"""Central tool registry with auto-discovery and unified execution."""

import importlib
import inspect
import logging
import os
import pkgutil

from plugin.framework.tool_base import ToolBase
from plugin.framework.schema_convert import to_openai_schema, to_mcp_schema

log = logging.getLogger("localwriter.tools")


# Global visibility toggles
EXPOSE_BATCH_TO_CHATBOT = False


class ToolRegistry:
    """Discovers, registers, and dispatches tools.

    Tools are auto-discovered from each module's ``tools/`` subpackage
    and registered here. Both the chatbot and MCP server use this single
    registry.
    """

    def __init__(self, services):
        self._services = services
        self._tools = {}  # name -> ToolBase instance
        self.batch_mode = False  # suppress per-tool cache invalidation

    # ── Registration ──────────────────────────────────────────────────

    def register(self, tool):
        """Register a single ToolBase instance."""
        if tool.name in self._tools:
            log.warning("Tool already registered, replacing: %s", tool.name)
        self._tools[tool.name] = tool

    def register_many(self, tools):
        for t in tools:
            self.register(t)

    def discover(self, package_path, package_name):
        """Auto-discover ToolBase subclasses in a package directory.

        Scans *package_path* for Python modules, imports them, and
        registers any concrete ToolBase subclass found.

        Args:
            package_path: Filesystem path to the package directory.
            package_name: Dotted Python package name (e.g. "plugin.modules.writer.tools").
        """
        if not os.path.isdir(package_path):
            return

        count = 0
        for importer, modname, ispkg in pkgutil.iter_modules([package_path]):
            if modname.startswith("_"):
                continue
            fqn = f"{package_name}.{modname}"
            try:
                mod = importlib.import_module(fqn)
            except Exception:
                log.exception("Failed to import tool module: %s", fqn)
                continue

            for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, ToolBase)
                    and obj is not ToolBase
                    and getattr(obj, "name", None)
                ):
                    try:
                        instance = obj()
                        self.register(instance)
                        count += 1
                    except Exception:
                        log.exception("Failed to instantiate tool: %s", obj)

        if count:
            log.info("Discovered %d tools from %s", count, package_name)

    # ── Lookup ────────────────────────────────────────────────────────

    def get(self, name):
        """Get a tool by name, or None."""
        return self._tools.get(name)

    def tools_for_doc_type(self, doc_type):
        """Return tools compatible with *doc_type* (or all if doc_type is None)."""
        for tool in self._tools.values():
            if tool.doc_types is None or doc_type in tool.doc_types:
                yield tool

    # ── Schema generation ─────────────────────────────────────────────

    def get_openai_schemas(self, doc_type=None, tier=None):
        """Return list of OpenAI function-calling schemas.

        When *tier* is set (e.g. ``"core"``), only tools with that tier
        are included.
        """
        tools = self.tools_for_doc_type(doc_type)
        if tier:
            tools = (t for t in tools if t.tier == tier)

        # Filter out batch tool for chatbot scenarios if requested
        if not EXPOSE_BATCH_TO_CHATBOT:
            tools = (t for t in tools if t.name != "execute_batch")

        return [to_openai_schema(t) for t in tools]


    def get_openai_schemas_by_names(self, names):
        """Return OpenAI schemas for specific tool *names*."""
        return [to_openai_schema(self._tools[n])
                for n in names if n in self._tools]

    def get_tool_summaries(self, doc_type=None, tier=None):
        """Lightweight catalogue: ``[{"name", "description", "tier"}]``."""
        tools = self.tools_for_doc_type(doc_type)
        if tier:
            tools = (t for t in tools if t.tier == tier)
        return [{"name": t.name,
                 "description": (t.description or "")[:120],
                 "tier": t.tier,
                 "intent": t.intent}
                for t in tools]

    def get_tool_names_by_intent(self, doc_type=None, intent=None):
        """Return names of extended tools matching *intent*."""
        return [t.name for t in self.tools_for_doc_type(doc_type)
                if t.tier == "extended" and t.intent == intent]

    def get_mcp_schemas(self, doc_type=None):
        """Return list of MCP tools/list schemas."""
        return [to_mcp_schema(t) for t in self.tools_for_doc_type(doc_type)]

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

        # Validate parameters
        ok, err = tool.validate(**kwargs)
        if not ok:
            return {"status": "error", "error": err}

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
        except Exception as exc:
            log.exception("Tool execution failed: %s", tool_name)
            if bus:
                bus.emit("tool:failed", name=tool_name, error=str(exc), caller=ctx.caller)
            return {"status": "error", "error": str(exc)}

        if bus:
            bus.emit("tool:completed", name=tool_name, caller=ctx.caller)

        return result

    @property
    def tool_names(self):
        return list(self._tools.keys())

    def __len__(self):
        return len(self._tools)
