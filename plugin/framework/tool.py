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
from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from typing import Any, cast, Callable

from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.schema_convert import to_openai_schema, to_mcp_schema
from plugin.framework.worker_pool import run_in_background

_log = logging.getLogger(__name__)
log = logging.getLogger("writeragent.tools")

_READ_PREFIXES = ("get_", "read_", "list_", "find_", "search_", "count_")


class ToolContext:
    """Immutable-ish context for a single tool invocation.

    Attributes:
        doc:       UNO document model.
        ctx:       UNO component context.
        doc_type:  Detected document type ("writer", "calc", "draw").
        services:  ServiceRegistry — access to all services.
        caller:    Who triggered the call ("chatbot", "mcp", "menu").
        status_callback: Optional callback for status updates (Writer tools).
        append_thinking_callback: Optional callback for thinking text (Writer tools).
        stop_checker: Optional callable () -> bool; if present and returns True, tool should stop.
        approval_callback: Optional callable for human-in-the-loop approval.
        chat_append_callback: Optional callable(str) to append plain text to the chat response.
        set_active_domain_callback: Optional callable to update the active domain.
    """

    doc: Any
    ctx: Any
    doc_type: str
    services: Any
    caller: str
    status_callback: Callable[[str], None] | None
    append_thinking_callback: Callable[[str], None] | None
    stop_checker: Callable[[], bool] | None
    approval_callback: Callable[[str], bool] | None
    chat_append_callback: Callable[[str], None] | None
    set_active_domain_callback: Callable[[str | None], None] | None

    __slots__ = ("doc", "ctx", "doc_type", "services", "caller", "status_callback", "append_thinking_callback", "stop_checker", "approval_callback", "chat_append_callback", "set_active_domain_callback")

    def __init__(self, doc, ctx, doc_type, services, caller="", status_callback=None, append_thinking_callback=None, stop_checker=None, approval_callback=None, chat_append_callback=None, set_active_domain_callback=None):
        self.doc = doc
        self.ctx = ctx
        self.doc_type = doc_type
        self.services = services
        self.caller = caller
        self.status_callback = status_callback
        self.append_thinking_callback = append_thinking_callback
        self.stop_checker = stop_checker
        self.approval_callback = approval_callback
        self.chat_append_callback = chat_append_callback
        self.set_active_domain_callback = set_active_domain_callback


class ToolBase(ABC):
    """Abstract base for every tool exposed to LLM agents and MCP clients.

    Subclasses must set ``name``, ``description``, ``parameters`` and
    implement ``execute``.

    Attributes:
        name:        Unique tool identifier (e.g. "get_document_tree").
        description: Human-readable description shown to LLMs.
        parameters:  JSON Schema dict (MCP ``inputSchema`` format).
        uno_services: List of UNO services the tool supports (e.g.,
                     ["com.sun.star.text.TextDocument"], or None for all).
        tier:        "core" = always sent to the LLM, "extended" = on demand
                     via the tool broker.  Default "extended".
        intent:      Broker group: "navigate", "edit", "review", or "media".
                     Used by request_tools(intent=...) to load tool groups.
        is_mutation:  Whether the tool mutates the document.  ``None``
                     means auto-detect from name prefix.
        long_running: Hint that the tool may take a while (e.g. image gen).
    """

    name: str | None = None
    description: str = ""
    parameters: dict | None = None
    uno_services: list | None = None
    tier: str = "extended"
    intent: str | None = None
    is_mutation: bool | None = None
    long_running: bool = False

    def detects_mutation(self):
        """Return True if the tool mutates the document."""
        if self.is_mutation is not None:
            return self.is_mutation
        if self.name:
            return not self.name.startswith(_READ_PREFIXES)
        return True

    def _tool_error(self, message, code="TOOL_EXECUTION_ERROR", **details):
        """Standardized JSON payload for tool errors.

        Args:
            message: User-friendly error message.
            code: Internal error code.
            **details: Optional context like tool_name, doc_type, etc.

        Returns:
            dict matching the standardized error format.
        """
        return format_error_payload(ToolExecutionError(message, code=code, details=details))

    def validate(self, **kwargs):
        """Validate arguments against ``parameters`` schema.

        Returns:
            (ok: bool, error_message: str | None)
        """
        schema = self.parameters or {}
        required = schema.get("required", [])
        for key in required:
            if key not in kwargs:
                return False, f"Missing required parameter: {key}"
        props = schema.get("properties", {})
        for key in kwargs:
            if props and key not in props:
                return False, f"Unknown parameter: {key}"
        return True, None

    @abstractmethod
    def execute(self, ctx: ToolContext, **kwargs) -> dict[str, Any]:
        """Execute the tool.

        Args:
            ctx:    ToolContext with doc, services, caller info.
            **kwargs: Tool arguments (already validated).

        Returns:
            dict with at least ``{"status": "ok"|"error", ...}``.
        """

    def is_async(self) -> bool:
        """Returns True if this tool should execute asynchronously in the background. Defaults to False."""
        return False

    def execute_safe(self, ctx: ToolContext, **kwargs) -> dict[str, Any]:
        """Execute with simple error containment."""
        try:
            # Check thread safety: If the tool is synchronous, it must not be called from a background thread
            # unless it's being executed through the QueueExecutor. Direct UNO calls from background threads
            # cause UI hangs and deadlocks in LibreOffice.
            if not self.is_async():
                if threading.current_thread() is not threading.main_thread():
                    raise RuntimeError(f"Thread Safety Violation: Synchronous tool '{self.name}' was executed from a background thread. Synchronous tools execute UNO APIs which are not thread-safe. You must wrap this call using `execute_on_main_thread` from `plugin.framework.queue_executor`.")
            return self.execute(ctx, **kwargs)
        except Exception as e:
            _log.exception("Tool '%s' execution failed", self.name if self.name else "<unknown>")
            return self._tool_error(f"Tool execution failed: {str(e)}", code="TOOL_EXECUTION_ERROR", original_error=str(e), error_type=type(e).__name__)

    def get_collection(self, doc, getter_name, missing_msg=None):
        """Helper to safely fetch a named collection from a document.

        Args:
            doc: UNO document object.
            getter_name: Method name to call (e.g., "getGraphicObjects").
            missing_msg: Error message if the document lacks the getter.

        Returns:
            The UNO collection object, or a dict with {"status": "error", "message": ...}
        """
        if not hasattr(doc, getter_name):
            msg = missing_msg or f"Document does not support {getter_name}."
            return self._tool_error(msg, code="UNO_OBJECT_ERROR", getter_name=getter_name)
        return getattr(doc, getter_name)()

    def get_item(self, doc, getter_name, item_name, missing_msg=None, not_found_msg=None):
        """Helper to fetch a specific item from a document's collection.

        Args:
            doc: UNO document object.
            getter_name: Method name to call (e.g., "getTextFrames").
            item_name: Name of the item to retrieve.
            missing_msg: Error message if the collection getter is missing.
            not_found_msg: Error message if the item doesn't exist.

        Returns:
            The UNO item object, or a dict with {"status": "error", "message": ..., "available": [...]}
        """
        collection = self.get_collection(doc, getter_name, missing_msg)
        if isinstance(collection, dict):
            return collection

        if not collection.hasByName(item_name):
            available = list(collection.getElementNames())
            msg = not_found_msg or f"Item '{item_name}' not found."
            return self._tool_error(msg, code="UNO_OBJECT_ERROR", item_name=item_name, getter_name=getter_name, available=available)

        return collection.getByName(item_name)


class ToolBaseDummy:
    """Marker base for temporarily disabled tools.

    Classes deriving from this base are intentionally **not** treated as
    tools by the registry. To re-enable a tool, change its base class
    back to ``ToolBase``.
    """

    name: str | None = None

    def _tool_error(self, message, code="TOOL_EXECUTION_ERROR", **details):
        """Standardized JSON payload for tool errors."""
        return format_error_payload(ToolExecutionError(message, code=code, details=details))

    def get_collection(self, doc, getter_name, missing_msg=None):
        """Helper to safely fetch a named collection from a document."""
        if not hasattr(doc, getter_name):
            msg = missing_msg or f"Document does not support {getter_name}."
            return self._tool_error(msg, code="UNO_OBJECT_ERROR", getter_name=getter_name)
        return getattr(doc, getter_name)()

    def get_item(self, doc, getter_name, item_name, missing_msg=None, not_found_msg=None):
        """Helper to fetch a specific item from a document's collection."""
        collection = self.get_collection(doc, getter_name, missing_msg)
        if isinstance(collection, dict):
            return collection
        if not collection.hasByName(item_name):
            available = list(collection.getElementNames())
            msg = not_found_msg or f"Item '{item_name}' not found."
            return self._tool_error(msg, code="UNO_OBJECT_ERROR", item_name=item_name, getter_name=getter_name, available=available)
        return collection.getByName(item_name)


def _is_specialized_domain_tool(t: Any, active_domain: str) -> bool:
    """True if *t* is a Writer/Calc/Draw specialized tool for *active_domain*."""
    if getattr(t, "specialized_domain", None) != active_domain:
        return False
    from plugin.modules.writer.base import ToolWriterSpecialBase
    from plugin.modules.calc.base import ToolCalcSpecialBase
    from plugin.modules.draw.base import ToolDrawSpecialBase

    return isinstance(t, (ToolWriterSpecialBase, ToolCalcSpecialBase, ToolDrawSpecialBase))


# Hidden from default chat/MCP tool lists; exposed via delegate_to_specialized_writer_toolset.
_DEFAULT_EXCLUDE_TIERS = frozenset({"specialized", "specialized_control"})
_UNSET_EXCLUDE_TIERS = object()


class ToolRegistry:
    """Registers and dispatches tools.

    Both the chatbot and MCP server use this single registry.
    """

    def __init__(self, services):
        self._services = services
        self._tools = {}  # name -> ToolBase instance
        self.batch_mode = False  # suppress per-tool cache invalidation

    # ── Registration ──────────────────────────────────────────────────

    def register(self, tool: ToolBase):
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
            # Normal repeated imports during auto-discovery can re-register the
            # same-named tool class from different module instances.
            # Only warn when the *class name* differs to avoid noise.
            if type(existing_tool).__name__ != type(tool).__name__:
                log.warning("Tool '%s' already registered (class %s), replacing with class %s", tool.name, type(existing_tool).__name__, type(tool).__name__)
        self._tools[tool.name] = tool

    def register_many(self, tools: list[ToolBase]):
        for t in tools:
            self.register(t)

    def auto_discover_package(self, package_name: str):
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

        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Must inherit from ToolBase, but not be ToolBase itself or ToolBaseDummy.
            # ToolBaseDummy is our way of easily disabling a tool if we don't think it's
            # worth having exposed to the AI, so we explicitly skip registering them.
            # Must be defined in this module to avoid double registration from imports
            # Also exclude abstract classes or classes without a defined 'name'
            if issubclass(obj, ToolBase) and obj is not ToolBase and not issubclass(obj, ToolBaseDummy) and obj.__module__ == module.__name__ and not inspect.isabstract(obj) and getattr(obj, "name", None):
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

    def get_tools(self, doc=None, doc_type=None, tier=None, intent=None, names=None, filter_doc_type=True, exclude_tiers=_UNSET_EXCLUDE_TIERS, active_domain=None):
        """Return a list of ToolBase instances matching the given criteria.

        Args:
            doc: Optional document model instance to check against uno_services.
            doc_type: Optional string indicating compatibility (deprecated, use doc). If None, only universal tools are returned (unless filter_doc_type=False).
            tier: Optional string (e.g. "core", "extended").
            intent: Optional string filtering by tool intent.
            names: Optional list of specific tool names to include.
            filter_doc_type: If True, filters by doc model services or doc_type. Defaults to True.
            exclude_tiers: Tiers to omit from the result. If omitted, excludes
                ``specialized`` and ``specialized_control`` so nested Writer tools
                stay off the main tool list. Pass ``()`` or ``frozenset()`` to include all tiers.
            active_domain: If provided, dynamically includes specialized tools for this domain
                and the specialized_workflow_finished tool.
        """
        tools = self._tools.values()

        # Helper to check if a tool supports the document
        def supports_doc(t):
            if not filter_doc_type:
                return True

            has_uno = hasattr(t, "uno_services") and t.uno_services is not None
            has_types = hasattr(t, "doc_types") and t.doc_types is not None

            is_supported = False
            if has_uno:
                if doc is not None and hasattr(doc, "supportsService"):
                    for svc in t.uno_services:
                        try:
                            if doc.supportsService(svc):
                                is_supported = True
                                break
                        except Exception:
                            pass
                if is_supported:
                    return True

            if has_types:
                if doc_type is not None and doc_type in t.doc_types:
                    return True
                return False

            return not has_uno

        tools = [t for t in tools if supports_doc(t)]

        # If we have an active domain, we want to include its tools (and the finish tool),
        # even if they are in the excluded tiers.

        if exclude_tiers is _UNSET_EXCLUDE_TIERS:
            to_exclude = _DEFAULT_EXCLUDE_TIERS
        else:
            import typing

            to_exclude = frozenset(cast("typing.Iterable[typing.Any]", exclude_tiers)) if exclude_tiers else frozenset()

        if active_domain:
            # If an active domain is set, restrict the list ONLY to the specialized tools
            # for that domain and the finish tool. Do not include normal core/extended tools.
            filtered_tools = []
            for t in tools:
                if _is_specialized_domain_tool(t, active_domain):
                    filtered_tools.append(t)
                # FIXME, these strings should be calculated or handled another way
                elif getattr(t, "name", "") in ["final_answer", "specialized_workflow_finished", "reply_to_user"]:
                    filtered_tools.append(t)
            tools = filtered_tools
        else:
            if to_exclude:

                def _tier_excluded(t):
                    tier = getattr(t, "tier", None)
                    return tier in to_exclude

                tools = [t for t in tools if not _tier_excluded(t)]

        if tier:
            tools = [t for t in tools if t.tier == tier]
        if intent:
            tools = [t for t in tools if t.intent == intent]
        if names:
            tools = [t for t in tools if t.name in names]
        return list(tools)

    def get_schemas(self, protocol="openai", active_domain=None, **kwargs):
        """Return schemas for tools matching the given kwargs criteria.

        Args:
            protocol: Either "openai" or "mcp".
            active_domain: Optional active specialized domain.
            **kwargs: Filters passed to get_tools().
        """
        tools = self.get_tools(active_domain=active_domain, **kwargs)
        if protocol == "openai":
            return [to_openai_schema(t) for t in tools]
        elif protocol == "mcp":
            return [to_mcp_schema(t) for t in tools]
        else:
            raise ValueError(f"Unknown protocol: {protocol}")

    def get_tool_summaries(self, **kwargs):
        """Lightweight catalogue: ``[{"name", "description", "tier", "intent"}]``."""
        tools = self.get_tools(**kwargs)
        return [{"name": t.name, "description": (t.description or "")[:120], "tier": t.tier, "intent": t.intent} for t in tools]

    def get(self, name: str) -> ToolBase | None:
        """Get a tool by name, or None."""
        return self._tools.get(name)

    # ── Execution ─────────────────────────────────────────────────────

    def _get_tool_timeout(self, tool: ToolBase):
        return getattr(tool, "timeout", 0)

    def _execute_with_timeout(self, func, timeout, tool_name="<unknown>", run_threaded=True, **kwargs):
        """Run *func* with an optional wall-clock timeout.

        If ``run_threaded`` is False (e.g. the tool is synchronous and its
        ``execute_safe`` main-thread guard would fire in a background thread),
        the timeout is ignored and the function runs inline. A warning is
        logged so misconfigured tools are visible.
        """
        if timeout <= 0:
            return func(**kwargs)

        if not run_threaded:
            log.warning("Tool '%s' declares timeout=%s but is synchronous; timeout is ignored (would trip the main-thread guard). Set is_async() to True to enable timeout enforcement.", tool_name, timeout)
            return func(**kwargs)

        result_queue: queue.Queue = queue.Queue()

        def worker():
            try:
                result_queue.put(("success", func(**kwargs)))
            except Exception as e:
                result_queue.put(("error", e))

        worker_thread = run_in_background(worker, name=f"tool-timeout-{tool_name}")
        worker_thread.join(timeout=timeout)

        if worker_thread.is_alive():
            return {"status": "error", "code": "TOOL_TIMEOUT", "message": f"Tool timed out after {timeout} seconds"}

        result_type, result = result_queue.get()
        if result_type == "error":
            raise result  # Will be caught by outer try/except

        return result

    def execute(self, tool_name: str, ctx: ToolContext, *, bypass_thread_guard: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Execute a tool by name.

        Args:
            tool_name: Registered tool name.
            ctx:       ToolContext for this invocation.
            bypass_thread_guard: If True, call ``tool.execute`` directly (no main-thread check).
                Used only by ``scripts/prompt_optimization/tools_lo`` where UNO runs on a dedicated
                LibreOffice worker thread (not Python's ``main_thread()``).
            **kwargs:  Tool arguments.

        Returns:
            dict: Result from the tool execution (typically a ToolResult).
        """
        try:
            tool = self._tools.get(tool_name)
            if tool is None:
                raise KeyError(f"Unknown tool: {tool_name}")

            # Check document compatibility using uno_services or fallback doc_types
            is_supported = False
            has_uno = hasattr(tool, "uno_services") and tool.uno_services is not None
            has_types = hasattr(tool, "doc_types") and tool.doc_types is not None

            if has_uno:
                if ctx.doc and hasattr(ctx.doc, "supportsService"):
                    for svc in tool.uno_services:
                        try:
                            if ctx.doc.supportsService(svc):
                                is_supported = True
                                break
                        except Exception:
                            pass

            if not is_supported and has_types:
                if ctx.doc_type and ctx.doc_type in tool.doc_types:
                    is_supported = True

            if not has_uno and not has_types:
                is_supported = True  # universal tool

            if not is_supported:
                raise ValueError(f"Tool {tool_name} does not support the current document")

            # Restrict kwargs to this tool's schema so extra keys (e.g. image_model
            # from API/LLM) do not cause "Unknown parameter" validation errors.
            props = (tool.parameters or {}).get("properties", {})
            if props:
                kwargs = {k: v for k, v in kwargs.items() if k in props}

            # Common context for all error details
            common_details = {"tool_name": tool_name}
            if ctx.caller:
                common_details["caller"] = ctx.caller
            if ctx.doc_type:
                common_details["doc_type"] = ctx.doc_type

            # Validate parameters
            ok, err = tool.validate(**kwargs)
            if not ok:
                return {"status": "error", "code": "VALIDATION_ERROR", "message": err, "details": common_details}

            # Emit executing event
            bus = self._services.get("events") if hasattr(self, "_services") and self._services else None
            if bus:
                bus.emit("tool:executing", name=tool_name, caller=ctx.caller)

            # Execution with simple isolation and timeout.
            # Threaded timeout is only safe when either the guard is bypassed
            # or the tool is explicitly async (otherwise execute_safe's
            # main-thread check would fail in the worker thread).
            runner = tool.execute if bypass_thread_guard else tool.execute_safe
            run_threaded = bypass_thread_guard or bool(tool.is_async())
            result = self._execute_with_timeout(runner, timeout=self._get_tool_timeout(tool), tool_name=tool_name, run_threaded=run_threaded, ctx=ctx, **kwargs)

            # Ensure any returned dict with status='error' includes full context details
            if isinstance(result, dict) and result.get("status") == "error":
                result_details = result.get("details", {})
                if isinstance(result_details, dict):
                    # merge common_details into result_details without overwriting existing keys
                    merged: dict[str, Any] = dict(result_details)
                    for k, v in common_details.items():
                        if k not in merged:
                            merged[k] = v
                    cast("dict[str, Any]", result)["details"] = merged

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
            return {"status": "error", "code": "TOOL_REGISTRY_ERROR", "message": f"Failed to execute tool '{tool_name}'", "details": {"tool_name": tool_name, "error": str(e), "type": type(e).__name__}}

    @property
    def tool_names(self):
        return list(self._tools.keys())

    def __len__(self):
        return len(self._tools)
