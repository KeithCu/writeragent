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

import copy
import logging
import queue
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, cast

from plugin.framework.errors import make_tool_error
from plugin.framework.worker_pool import run_in_background
from plugin.framework.thread_guard import assert_main_thread


def _normalize_schema_for_strict_providers(params):
    """Normalize JSON Schema so strict providers (e.g. Gemini via OpenRouter) accept it.

    - Union types (e.g. \"type\": [\"string\", \"array\"]) are replaced with the first type.
    - Empty \"required\" is removed so providers do not complain about required[0/1] missing.
    - Nested properties are normalized recursively.
    """
    if not params or not isinstance(params, dict):
        return params
    params = copy.deepcopy(params)
    if "type" in params and isinstance(params["type"], list):
        types = params["type"]
        params["type"] = "array" if "array" in types else (types[0] if types else "string")
    if params.get("type") != "array":
        params.pop("items", None)
    if params.get("required") == []:
        params.pop("required", None)
    for key in ("properties", "items"):
        if key in params and isinstance(params[key], dict):
            if key == "properties":
                for k, v in params[key].items():
                    params[key][k] = _normalize_schema_for_strict_providers(v)
            else:
                params[key] = _normalize_schema_for_strict_providers(params[key])
        elif key in params and isinstance(params[key], list):
            # items can be a list of schemas in JSON Schema; take first
            if params[key]:
                params[key] = _normalize_schema_for_strict_providers(params[key][0])
    return params


def _doc_type_str_from_doc(doc: Any) -> str | None:
    """Map UNO document model to tool context doc_type string."""
    if doc is None:
        return None
    try:
        from plugin.doc.document_helpers import DocumentType, get_document_type

        dt = get_document_type(doc)
        if dt == DocumentType.CALC:
            return "calc"
        if dt in (DocumentType.DRAW, DocumentType.IMPRESS):
            return "draw"
        if dt == DocumentType.WRITER:
            return "writer"
    except Exception:
        pass
    return None


def to_openai_schema(tool, *, doc_type: str | None = None):
    """Convert a ToolBase instance to an OpenAI function-calling schema.

    Returns::

        {
            "type": "function",
            "function": {
                "name": "get_document_tree",
                "description": "...",
                "parameters": { ... JSON Schema ... }
            }
        }
    """
    params = copy.deepcopy(tool.get_parameters(doc_type) or {})
    if "type" not in params:
        params["type"] = "object"
    params = _normalize_schema_for_strict_providers(params)
    desc = tool.get_description(doc_type)

    return {"type": "function", "function": {"name": tool.name, "description": desc, "parameters": params}}


def to_mcp_schema(tool, *, doc_type: str | None = None):
    """Convert a ToolBase instance to an MCP tools/list schema.

    Returns::

        {
            "name": "get_document_outline",
            "description": "...",
            "inputSchema": { ... JSON Schema ... }
        }
    """
    input_schema = copy.deepcopy(tool.get_parameters(doc_type) or {})
    if "type" not in input_schema:
        input_schema["type"] = "object"
    if "properties" not in input_schema:
        input_schema["properties"] = {}
    if "document_url" not in input_schema["properties"]:
        input_schema["properties"]["document_url"] = {
            "type": "string",
            "description": "Optional URL of the target document. If not provided, the active document is used."
        }
    desc = tool.get_description(doc_type)

    agent_label = getattr(tool, "_agent_label", None)
    special_base = getattr(tool, "_special_base_class", None)
    if agent_label and special_base is not None:
        from plugin.framework.constants import format_specialized_domains_description

        # For MCP schemas, use a compact description to avoid duplicating the long domain list
        # (the detailed domain guidance lives in the 'domain' property description instead).
        # The full verbose guidance with examples is still used in chat system prompts.
        desc = (
            f"{desc} Delegates to a specialized {agent_label} task. "
            "See the 'domain' property for available areas and the 'task' parameter rules."
        ).strip()

        props = input_schema.get("properties")
        if isinstance(props, dict) and "domain" in props and isinstance(props["domain"], dict):
            props["domain"]["description"] = format_specialized_domains_description(special_base, agent_label=agent_label)

    return {"name": tool.name, "description": desc, "inputSchema": input_schema}


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
        read_only_target: When True, mutation tools are rejected (document_research sibling reads).
        send_cancellation: Optional per-send :class:`~plugin.framework.queue_executor.SendCancellation`
            for worker-thread HTTP registration and stable stop checks.
    """

    doc: Any
    ctx: Any
    doc_type: str
    services: Any
    caller: str
    active_page_index: int | None
    status_callback: Callable[[str], None] | None
    append_thinking_callback: Callable[[str], None] | None
    stop_checker: Callable[[], bool] | None
    approval_callback: Callable[[str], bool] | None
    chat_append_callback: Callable[[str], None] | None
    set_active_domain_callback: Callable[[str | None], None] | None
    active_domain: str | None
    python_tool_domain: str | None
    read_only_target: bool
    send_cancellation: Any | None

    __slots__ = ("doc", "ctx", "doc_type", "services", "caller", "active_page_index", "status_callback", "append_thinking_callback", "stop_checker", "approval_callback", "chat_append_callback", "set_active_domain_callback", "active_domain", "python_tool_domain", "read_only_target", "send_cancellation")

    def __init__(self, doc, ctx, doc_type, services, caller="", active_page_index=None, status_callback=None, append_thinking_callback=None, stop_checker=None, approval_callback=None, chat_append_callback=None, set_active_domain_callback=None, active_domain=None, python_tool_domain=None, read_only_target=False, send_cancellation=None):
        self.doc = doc
        self.ctx = ctx
        self.doc_type = doc_type
        self.services = services
        self.caller = caller
        self.active_page_index = active_page_index
        self.status_callback = status_callback
        self.append_thinking_callback = append_thinking_callback
        self.stop_checker = stop_checker
        self.approval_callback = approval_callback
        self.chat_append_callback = chat_append_callback
        self.set_active_domain_callback = set_active_domain_callback
        self.active_domain = active_domain
        self.python_tool_domain = python_tool_domain
        self.read_only_target = read_only_target
        self.send_cancellation = send_cancellation
        if send_cancellation is not None and stop_checker is None:
            self.stop_checker = send_cancellation.is_cancelled


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
        tier:        Main chat and MCP default lists use ``"core"``. Nested
                     specialized toolsets use ``"specialized"`` or
                     ``"specialized_control"`` (hidden from default lists via
                     ``exclude_tiers``). Default ``"core"``.
        intent:      Optional group label (e.g. "navigate", "edit", "review",
                     "media") for ``get_tools(intent=...)`` filtering.
        is_mutation:  Whether the tool mutates the document.  ``None``
                     means auto-detect from name prefix.
        long_running: Hint that the tool may take a while (e.g. image gen).
    """

    name: str | None = None
    description: str = ""
    parameters: dict | None = None
    uno_services: list | None = None
    tier: str = "core"
    intent: str | None = None
    is_mutation: bool | None = None
    long_running: bool = False
    is_final_answer_tool: bool = False
    doc_types: list[str] | None = None
    required_core_tools: ClassVar[frozenset[str] | None] = None

    def detects_mutation(self):
        """Return True if the tool mutates the document."""
        if self.is_mutation is not None:
            return self.is_mutation
        if self.name:
            return not self.name.startswith(_READ_PREFIXES)
        return True

    def requires_document_lock(self, arguments=None):
        """Whether a long-running or backpressure MCP run must hold the per-document gate.

        Defaults to :meth:`detects_mutation`. Override when a tool is sometimes read-only
        depending on ``arguments`` (e.g. delegate gateway domains). See
        docs/threading_architecture.md § MCP tool execution paths.
        """
        return self.detects_mutation()

    def _tool_error(self, message, code="TOOL_EXECUTION_ERROR", **details):
        """Standardized JSON payload for tool errors.

        Delegates to the central make_tool_error factory so every tool
        error path (including the Dummy base and Registry) produces
        identical structure. See errors.py:make_tool_error for the
        single source of truth (added during 2026 error formatting
        centralization).
        """
        return make_tool_error(message, code=code, **details)

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        """JSON Schema for this tool; override for document-type-specific parameters."""
        return self.parameters

    def get_description(self, doc_type: str | None = None) -> str:
        """Tool description for the LLM; override when ``get_parameters`` varies by doc type."""
        return self.description or ""

    def validate(self, *, doc_type: str | None = None, **kwargs):
        """Validate arguments against ``parameters`` schema.

        Returns:
            (ok: bool, error_message: str | None)
        """
        schema = self.get_parameters(doc_type) or {}
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
            # Single source of truth for the synchronous-tool main-thread rule (Layer A).
            # The runtime guard (thread_guard) raises (or warns) with task identity when enabled.
            # bypass_thread_guard is honored at the call site in ToolRegistry.execute (it calls .execute directly).
            if not self.is_async():
                assert_main_thread(self.name or "synchronous tool")
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
    is_final_answer_tool: bool = False

    def _tool_error(self, message, code="TOOL_EXECUTION_ERROR", **details):
        """Standardized JSON payload for tool errors.

        Delegates to the central make_tool_error (see the real ToolBase
        implementation and errors.make_tool_error). This removes the
        previous near-duplicate.
        """
        return make_tool_error(message, code=code, **details)

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
    # Support composite domains like "python:writer"
    active_domain_base = active_domain.split(":")[0] if ":" in active_domain else active_domain
    tool_domain = getattr(t, "specialized_domain", None)

    if tool_domain != active_domain_base:
        # If the tool matches the subdomain exactly, and we are in a composite domain, include it.
        if ":" in active_domain:
            subdomain = active_domain.split(":")[1]
            if tool_domain == subdomain:
                return True
        return False
    # Cross-app specialized tools (e.g. external venv Python) register once but must
    # appear under delegate_to_specialized_writer/calc/draw_toolset(domain=...) for any doc.
    if getattr(t, "specialized_cross_cutting", False):
        return True
    from plugin.writer.specialized_base import ToolWriterSpecialBase
    from plugin.calc.base import ToolCalcSpecialBase
    from plugin.draw.base import ToolDrawSpecialBase

    return isinstance(t, (ToolWriterSpecialBase, ToolCalcSpecialBase, ToolDrawSpecialBase))


# Hidden from default chat/MCP tool lists; exposed via delegate_to_specialized_writer_toolset.
_DEFAULT_EXCLUDE_TIERS = frozenset({"specialized", "specialized_control", "mcp"})
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
            tier: Optional string; main chat tools use ``"core"``.
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

            is_supported = False
            if t.uno_services is not None:
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

            if t.doc_types is not None:
                if doc_type is not None and doc_type in t.doc_types:
                    return True
                return False

            return t.uno_services is None

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
            # for that domain and the finish tool. Do not include normal default-tier tools.
            # However, we also include any core tools explicitly requested by the domain.

            # First, find which core tools are required by any tool in this domain
            required_core = set()
            for t in tools:
                if _is_specialized_domain_tool(t, active_domain):
                    req = getattr(t, "required_core_tools", None)
                    if req:
                        required_core.update(req)

            from plugin.framework.constants import WRITER_SIDEBAR_ONLY_DOMAINS

            filtered_tools = []
            for t in tools:
                if _is_specialized_domain_tool(t, active_domain):
                    filtered_tools.append(t)
                elif t.name == "specialized_workflow_finished" and active_domain not in WRITER_SIDEBAR_ONLY_DOMAINS:
                    # Sidebar-only domains (brainstorming, writing_plan) use bespoke finish tools.
                    filtered_tools.append(t)
                # Dynamically include core tools required for this domain
                elif getattr(t, "tier", None) == "core" and t.name in required_core:
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
        doc_type = kwargs.get("doc_type") or _doc_type_str_from_doc(kwargs.get("doc"))
        if protocol == "openai":
            return [to_openai_schema(t, doc_type=doc_type) for t in tools]
        elif protocol == "mcp":
            return [to_mcp_schema(t, doc_type=doc_type) for t in tools]
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
            if tool.uno_services is not None:
                if ctx.doc and hasattr(ctx.doc, "supportsService"):
                    for svc in tool.uno_services:
                        try:
                            if ctx.doc.supportsService(svc):
                                is_supported = True
                                break
                        except Exception:
                            pass

            if not is_supported and tool.doc_types is not None:
                if ctx.doc_type and ctx.doc_type in tool.doc_types:
                    is_supported = True

            if tool.uno_services is None and tool.doc_types is None:
                is_supported = True  # universal tool

            if not is_supported:
                raise ValueError(f"Tool {tool_name} does not support the current document")

            # Restrict kwargs to this tool's schema so extra keys (e.g. image_model
            # from API/LLM) do not cause "Unknown parameter" validation errors.
            schema = tool.get_parameters(ctx.doc_type) or {}
            props = (schema or {}).get("properties", {})
            if props:
                kwargs = {k: v for k, v in kwargs.items() if k in props}

            # Common context for all error details
            common_details = {"tool_name": tool_name}
            if ctx.caller:
                common_details["caller"] = ctx.caller
            if ctx.doc_type:
                common_details["doc_type"] = ctx.doc_type

            # Validate parameters
            ok, err = tool.validate(doc_type=ctx.doc_type, **kwargs)
            if not ok:
                return {"status": "error", "code": "VALIDATION_ERROR", "message": err, "details": common_details}

            if getattr(ctx, "read_only_target", False) and tool.detects_mutation():
                # Use the central factory (all tool errors now go through make_tool_error).
                return make_tool_error(
                    "This document is open for read-only document_research access; writes are not allowed.",
                    code="READ_ONLY_TARGET",
                    **common_details,
                )

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
