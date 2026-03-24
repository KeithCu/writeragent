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
import threading
import queue

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
        # Validate tool schema
        if not tool.name or not isinstance(tool.name, str):
            log.error("Failed to register tool '%s': missing or invalid name.", type(tool).__name__)
            return
        if not tool.description or not isinstance(tool.description, str):
            log.error("Failed to register tool '%s': missing or invalid description.", tool.name)
            return
        if tool.parameters is not None and not isinstance(tool.parameters, dict):
            log.error("Failed to register tool '%s': parameters must be a dictionary or None.", tool.name)
            return

        if tool.name in self._tools:
            # If it's the exact same class, skip silently.
            existing_tool = self._tools[tool.name]
            if type(existing_tool) is type(tool):
                return
            log.warning("Tool '%s' already registered (class %s), replacing with class %s",
                        tool.name, type(existing_tool).__name__, type(tool).__name__)
        self._tools[tool.name] = tool

    def register_many(self, tools):
        for t in tools:
            self.register(t)

    def auto_discover_package(self, package_name):
        """Automatically discover and register ToolBase subclasses in all submodules of a package."""
        import importlib
        import pkgutil

        # Import the package itself to get its path
        package = importlib.import_module(package_name)

        # Iterate over all submodules in the package directory
        for _, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
            full_module_name = f"{package_name}.{module_name}"
            try:
                module = importlib.import_module(full_module_name)
                self.auto_discover(module)
            except ImportError as e:
                log.error("Failed to import module %s for tool discovery: %s", full_module_name, e)
            except AttributeError as e:
                log.error("Module attribute error during tool discovery in %s: %s", full_module_name, e)
            except Exception as e:
                log.error("Unexpected error during tool discovery in %s: %s", full_module_name, e)

    def auto_discover(self, module):
        """Automatically discover and register ToolBase subclasses in a module."""
        import inspect
        from plugin.framework.tool_base import ToolBase, ToolBaseDummy

        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Must inherit from ToolBase, but not be ToolBase itself or ToolBaseDummy.
            # ToolBaseDummy is our way of easily disabling a tool if we don't think it's
            # worth having exposed to the AI, so we explicitly skip registering them.
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
                except TypeError as e:
                    log.error("Failed to instantiate tool %s (TypeError): %s", obj.__name__, e)
                except ValueError as e:
                    log.error("Failed to instantiate tool %s (ValueError): %s", obj.__name__, e)
                except Exception as e:
                    log.error("Failed to instantiate tool %s: %s", obj.__name__, e)

    # ── Lookup & Schema Generation ────────────────────────────────────

    def get_tools(self, doc=None, doc_type=None, tier=None, intent=None, names=None, filter_doc_type=True):
        """Return a list of ToolBase instances matching the given criteria.

        Args:
            doc: Optional document model instance to check against uno_services.
            doc_type: Optional string indicating compatibility (deprecated, use doc). If None, only universal tools are returned (unless filter_doc_type=False).
            tier: Optional string (e.g. "core", "extended").
            intent: Optional string filtering by tool intent.
            names: Optional list of specific tool names to include.
            filter_doc_type: If True, filters by doc model services or doc_type. Defaults to True.
        """
        tools = self._tools.values()

        # Helper to check if a tool supports the document
        def supports_doc(t):
            if not filter_doc_type:
                return True

            # Use uno_services if available and doc is provided
            if hasattr(t, "uno_services") and t.uno_services is not None:
                if doc is not None and hasattr(doc, "supportsService"):
                    for svc in t.uno_services:
                        try:
                            if doc.supportsService(svc):
                                return True
                        except Exception:
                            pass
                return False

            # Fallback to legacy doc_types
            if hasattr(t, "doc_types") and t.doc_types is not None:
                if doc_type is not None and doc_type in t.doc_types:
                    return True
                return False

            # Universal tool (both uno_services and doc_types are None)
            return True

        tools = [t for t in tools if supports_doc(t)]

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

    def _get_tool_timeout(self, tool):
        return getattr(tool, "timeout", 0)

    def _execute_with_timeout(self, func, timeout, **kwargs):
        """Simple timeout handling."""
        if timeout <= 0:
            return func(**kwargs)

        # Use simple threading for timeout
        result_queue = queue.Queue()

        def worker():
            try:
                result = func(**kwargs)
                result_queue.put(('success', result))
            except Exception as e:
                result_queue.put(('error', e))

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=timeout)

        if worker_thread.is_alive():
            return {
                "status": "error",
                "code": "TOOL_TIMEOUT",
                "message": f"Tool timed out after {timeout} seconds"
            }

        result_type, result = result_queue.get()
        if result_type == 'error':
            raise result  # Will be caught by outer try/except

        return result

    def execute(self, tool_name, ctx, **kwargs):
        """Execute a tool by name.

        Args:
            tool_name: Registered tool name.
            ctx:       ToolContext for this invocation.
            **kwargs:  Tool arguments.

        Returns:
            dict result from the tool.
        """
        try:
            tool = self._tools.get(tool_name)
            if tool is None:
                raise KeyError(f"Unknown tool: {tool_name}")

            # Check document compatibility using uno_services or fallback doc_types
            is_supported = False
            if hasattr(tool, "uno_services") and tool.uno_services is not None:
                if ctx.doc and hasattr(ctx.doc, "supportsService"):
                    for svc in tool.uno_services:
                        try:
                            if ctx.doc.supportsService(svc):
                                is_supported = True
                                break
                        except Exception:
                            pass
            elif hasattr(tool, "doc_types") and tool.doc_types is not None:
                if ctx.doc_type and ctx.doc_type in tool.doc_types:
                    is_supported = True
            else:
                is_supported = True # universal tool

            if not is_supported:
                raise ValueError(
                    f"Tool {tool_name} does not support the current document"
                )

            # Restrict kwargs to this tool's schema so extra keys (e.g. image_model
            # from API/LLM) do not cause "Unknown parameter" validation errors.
            props = (tool.parameters or {}).get("properties", {})
            if props:
                kwargs = {k: v for k, v in kwargs.items() if k in props}

            from plugin.framework.errors import format_error_payload, ToolExecutionError

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
            bus = self._services.get("events") if hasattr(self, "_services") and self._services else None
            if bus:
                bus.emit("tool:executing", name=tool_name, caller=ctx.caller)

            # Execution with simple isolation and timeout
            result = self._execute_with_timeout(
                tool.execute_safe,
                timeout=self._get_tool_timeout(tool),
                ctx=ctx,
                **kwargs
            )

            # Ensure any returned dict with status='error' includes full context details
            if isinstance(result, dict) and result.get("status") == "error":
                result_details = result.get("details", {})
                if isinstance(result_details, dict):
                    # merge common_details into result_details without overwriting existing keys
                    for k, v in common_details.items():
                        if k not in result_details:
                            result_details[k] = v
                    result["details"] = result_details

            if bus:
                # only emit completed if result was not an error (optional, but follows general pattern)
                if not (isinstance(result, dict) and result.get("status") == "error"):
                    bus.emit("tool:completed", name=tool_name, caller=ctx.caller)
                else:
                    bus.emit("tool:failed", name=tool_name, error=result.get("message"), caller=ctx.caller)

            return result

        except KeyError:
            raise
        except ValueError:
            raise
        except Exception as e:
            # Simple wrapping
            bus = self._services.get("events") if hasattr(self, "_services") and self._services else None
            log.exception("Tool execution failed: %s", tool_name)
            if bus:
                bus.emit("tool:failed", name=tool_name, error=str(e), caller=ctx.caller)
            return {
                "status": "error",
                "code": "TOOL_REGISTRY_ERROR",
                "message": f"Failed to execute tool '{tool_name}'",
                "details": {
                    "tool_name": tool_name,
                    "error": str(e),
                    "type": type(e).__name__
                }
            }

    @property
    def tool_names(self):
        return list(self._tools.keys())

    def __len__(self):
        return len(self._tools)
