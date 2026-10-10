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
"""Tool base types and the process-wide ToolRegistry.

Concurrency: tools are registered while the extension starts (main
thread), then looked up by name from chat and MCP. There is no lock on
the ``_tools`` dict — do not register from a worker. Synchronous tools
that touch the document are marshaled onto the LibreOffice UI thread
before they run. If a tool declares a timeout, ``execute`` starts a
**dedicated** background thread and ``join``s it; when the timer fires
that thread is **abandoned** if it has not queued a result (it may
still finish, and that late result is dropped). A result already on
the queue is returned — the thread can still be alive while it
unwinds. A worker that dies without queuing a result returns an error
instead of blocking. Python cannot kill a thread cleanly. Cooperative
cancel is ``SendCancellation`` (Stop sets a flag / closes HTTP), not
``thread.kill``.
"""

from __future__ import annotations

import logging
import queue
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, cast

from plugin.framework.errors import (
    WriterAgentException,
    format_error_payload,
    is_document_disposed,
    is_tool_document_disposed,
    make_tool_error,
)
from plugin.framework.worker_pool import run_in_background
from plugin.framework.thread_guard import assert_main_thread
from plugin.framework.queue_executor import execute_on_main_thread

from plugin.framework.deal_shim import DEAL_MAX_TOKEN, ascii_bounded, deal
from plugin.framework.tool_schema import _normalize_schema_for_strict_providers as _normalize_schema_for_strict_providers, call_properties as call_properties, coerce_call_args as coerce_call_args, to_mcp_schema as to_mcp_schema, to_openai_schema as to_openai_schema, without_unknown_kwargs as without_unknown_kwargs

_log = logging.getLogger(__name__)
log = logging.getLogger("writeragent.tools")


def _doc_type_str_from_doc(doc: Any) -> str | None:
    """Map UNO document model to tool context doc_type string."""
    # crosshair: off
    if doc is None:
        return None
    try:
        from plugin.doc.doc_type import doc_type_label_for_enum, get_document_type

        dt = get_document_type(doc)
        label = doc_type_label_for_enum(dt, impress_as_draw=True)
        return None if label == "unknown" else label
    except Exception:
        _log.exception("doc_type probe failed")
    return None


# verb_noun tools (legacy/core): name starts with a read verb.
_READ_PREFIXES = ("get_", "read_", "list_", "find_", "search_", "count_")
# domain_verb tools (specialized): a later token is a read verb (image_list, style_get_info).
_READ_NAME_TOKENS = frozenset({"list", "get", "read", "find", "search", "count", "info", "stats", "overview", "summary", "children", "surroundings", "tree", "outline", "recent"})


@deal.pre(lambda name: isinstance(name, str) and ascii_bounded(name, DEAL_MAX_TOKEN))
def _name_looks_readonly(name: str) -> bool:
    """True when the tool name implies a non-mutating read (prefix or domain_verb)."""
    if name.startswith(_READ_PREFIXES):
        return True
    parts = name.split("_")
    # domain_verb…: first token is the domain; any later read token => read-only.
    return len(parts) >= 2 and any(p in _READ_NAME_TOKENS for p in parts[1:])


class ToolContext:
    """Immutable-ish context for a single tool invocation.

    Attributes:
        doc:       UNO document model.
        ctx:       UNO component context.
        doc_type:  Detected document type ("writer", "calc", "draw").
        services:  ServiceRegistry — access to all services.
        caller:    Who triggered the call ("chatbot", "mcp", "menu").
        active_page_index: Optional current page index in the view.
        active_domain: Optional domain string restricting tool access.
        python_tool_domain: Optional domain string for Python execution scopes.
        status_callback: Optional callback for status updates (Writer tools).
        append_thinking_callback: Optional callback for thinking text (Writer tools).
        stop_checker: Optional callable () -> bool; if present and returns True, tool should stop.
        approval_callback: Optional callable for human-in-the-loop approval.
        chat_append_callback: Optional callable(str) to append plain text to the chat response.
        set_active_domain_callback: Optional callable to update the active domain.
        read_only_target: When True, mutation tools are rejected (document_research sibling reads).
        send_cancellation: Optional per-send :class:`~plugin.framework.queue_executor.SendCancellation`
            for worker-thread HTTP registration and stable stop checks.
        uno_services_supported: Cached UNO service names for the active document (sidebar/MCP);
            used for tool compatibility without touching ``doc`` off the main thread.
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
    uno_services_supported: frozenset[str]

    __slots__: ClassVar[tuple[str, ...]] = (
        "doc",
        "ctx",
        "doc_type",
        "services",
        "caller",
        "active_page_index",
        "status_callback",
        "append_thinking_callback",
        "stop_checker",
        "approval_callback",
        "chat_append_callback",
        "set_active_domain_callback",
        "active_domain",
        "python_tool_domain",
        "read_only_target",
        "send_cancellation",
        "uno_services_supported",
    )

    def __init__(
        self,
        doc: Any,
        ctx: Any,
        doc_type: str,
        services: Any,
        caller: str = "",
        active_page_index: int | None = None,
        status_callback: Any = None,
        append_thinking_callback: Any = None,
        stop_checker: Any = None,
        approval_callback: Any = None,
        chat_append_callback: Any = None,
        set_active_domain_callback: Any = None,
        active_domain: str | None = None,
        python_tool_domain: str | None = None,
        read_only_target: bool = False,
        send_cancellation: Any = None,
        uno_services_supported: Any = None,
    ) -> None:
        # crosshair: off
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
        if uno_services_supported is not None:
            self.uno_services_supported = uno_services_supported
        else:
            from plugin.doc.doc_type import uno_services_for_doc_type_label

            self.uno_services_supported = uno_services_for_doc_type_label(doc_type)
        if send_cancellation is not None and stop_checker is None:
            self.stop_checker = send_cancellation.is_cancelled


def _tool_arg_matches_type(value: Any, schema_type: Any) -> bool:
    """True when *value* fits a JSON Schema ``type`` (string or union list).

    A missing type is accepted. An unknown name such as ``"str"`` matches
    nothing. Unions accept any listed JSON Schema member.
    """
    if schema_type is None:
        return True
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    for one in types:
        if one == "string" and isinstance(value, str):
            return True
        if one == "integer" and type(value) is int:
            return True
        if one == "number" and type(value) in (int, float):
            return True
        if one == "boolean" and type(value) is bool:
            return True
        if one == "array" and isinstance(value, list):
            return True
        if one == "object" and isinstance(value, dict):
            return True
        if one == "null" and value is None:
            return True
        # Unknown names (``"str"``) used to accept any value. They match nothing.
    return False


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
                     ``exclude_tiers``). Sidebar-only tools use ``"chat"``
                     (on the chat wire; hidden from MCP). Default ``"core"``.
        intent:      Optional group label (e.g. "navigate", "edit", "review",
                     "media") for ``get_tools(intent=...)`` filtering.
        is_mutation:  Whether the tool mutates the document.  ``None``
                     means auto-detect from a read prefix (``get_``,
                     ``list_``, …) or a later domain-verb token
                     (``image_list``, ``style_get_info``).
        long_running: Hint that the tool may take a while (e.g. image gen).
    """

    name: str | None = None
    description: str = ""
    parameters: dict[str, Any] | None = None
    uno_services: list[str] | None = None
    tier: str = "core"
    intent: str | None = None
    is_mutation: bool | None = None
    long_running: bool = False
    is_final_answer_tool: bool = False
    doc_types: list[str] | None = None
    # When False, the MCP executor runs the tool even with no document open (e.g. the
    # find_tools discovery meta-tool); the default requires an open document.
    requires_document: bool = True
    required_core_tools: ClassVar[frozenset[str] | None] = None

    def detects_mutation(self) -> bool:
        """Return True if the tool mutates the document."""
        if self.is_mutation is not None:
            return self.is_mutation
        if self.name:
            return not _name_looks_readonly(self.name)
        return True

    def requires_document_lock(self, arguments: Any = None) -> bool:
        """Whether a long-running or backpressure MCP run must hold the per-document gate.

        Defaults to :meth:`detects_mutation`. Override when a tool is sometimes read-only
        depending on ``arguments`` (e.g. delegate gateway domains). See
        docs/framework/threading.md § MCP tool execution paths.
        """
        return self.detects_mutation()

    def _tool_error(self, message: str, code: str = "TOOL_EXECUTION_ERROR", **details: Any) -> dict[str, Any]:
        """Standardized JSON payload for tool errors.

        Delegates to the central make_tool_error factory so every tool
        error path (including the Dummy base and Registry) produces
        identical structure. See errors.py:make_tool_error for the
        single source of truth (added during 2026 error formatting
        centralization).
        """
        return make_tool_error(message, code=code, **details)

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        """JSON Schema for this tool; override for document-type-specific parameters."""
        return self.parameters

    def get_description(self, doc_type: str | None = None) -> str:
        """Tool description for the LLM; override when ``get_parameters`` varies by doc type."""
        return self.description or ""

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[bool, str | None]:
        """Validate arguments against ``parameters`` schema.

        Returns:
            (ok: bool, error_message: str | None)
        """
        schema = self.get_parameters(doc_type) or {}
        required = schema.get("required", [])
        for key in required:
            if key not in kwargs:
                return False, f"Missing required parameter: {key}"
        extra_ok = getattr(self, "scripting_only_parameters", None) or frozenset()
        # Same allow-list as execute and the pre-shim check. Empty
        # properties {} is a closed schema, not "accept any kwargs".
        props_dict = call_properties(schema)
        for key in kwargs:
            if props_dict is not None and key not in props_dict and key not in extra_ok:
                return False, f"Unknown parameter: {key}"
        for key, value in kwargs.items():
            prop = props_dict.get(key) if props_dict is not None else None
            if not isinstance(prop, dict) or key in extra_ok:
                continue
            enum = prop.get("enum")
            if isinstance(enum, list) and enum and value not in enum:
                return False, f"Invalid value for {key}: {value!r}"
            if not _tool_arg_matches_type(value, prop.get("type")):
                return False, f"Invalid type for {key}: expected {prop.get('type')}, got {type(value).__name__}"
        return True, None

    @abstractmethod
    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Execute the tool.

        Args:
            ctx:    ToolContext with doc, services, caller info.
            **kwargs: Tool arguments (already validated).

        Returns:
            dict with at least ``{"status": "ok"|"error", ...}``.
        """
        # crosshair: off

    def is_async(self) -> bool:
        """Returns True if this tool should execute asynchronously in the background. Defaults to False."""
        return False

    def execute_safe(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Execute with simple error containment."""
        # crosshair: off
        try:
            # Defense in depth: ToolRegistry.execute marshals sync tools to the main thread;
            # this assert still catches direct execute_safe calls from background workers.
            # Only ToolRegistry.execute_unguarded skips this method. A model
            # argument named bypass_thread_guard is dropped by execute().
            if not self.is_async():
                assert_main_thread(self.name or "synchronous tool")
                if self.requires_document and ctx is not None and getattr(ctx, "doc", None) is not None:
                    if is_document_disposed(ctx.doc):
                        return self._tool_error("Document was closed or disposed by LibreOffice", code="DOCUMENT_DISPOSED")
            # Async / review-wait tools skip this probe so wait can run off the main thread.
            # EditReviewSession.wait_for_review must detect dispose itself. Do not "fix" by
            # running this check when is_async() is True.

            return self.execute(ctx, **kwargs)

        except Exception as e:
            _log.exception("Tool '%s' execution failed", self.name if self.name else "<unknown>")
            doc = getattr(ctx, "doc", None) if ctx is not None else None
            if is_tool_document_disposed(e, doc):
                return self._tool_error("Document was closed or disposed by LibreOffice", code="DOCUMENT_DISPOSED", original_error=str(e), error_type=type(e).__name__)
            # Returning _tool_error() already keeps the code. Raising
            # ToolPermissionError / ConfigError used to be rewritten as
            # TOOL_EXECUTION_ERROR and lose details.
            if isinstance(e, WriterAgentException):
                return format_error_payload(e)
            # Bare RuntimeException often has an empty message; fall back to the type name.
            err_msg = str(e).strip() or type(e).__name__
            return self._tool_error(f"Tool execution failed: {err_msg}", code="TOOL_EXECUTION_ERROR", original_error=str(e), error_type=type(e).__name__)

    def get_collection(self, doc: Any, getter_name: str, missing_msg: str | None = None) -> Any:
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

    def get_item(self, doc: Any, getter_name: str, item_name: str, missing_msg: str | None = None, not_found_msg: str | None = None) -> Any:
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

    def _tool_error(self, message: str, code: str = "TOOL_EXECUTION_ERROR", **details: Any) -> dict[str, Any]:
        """Standardized JSON payload for tool errors.

        We keep _tool_error on the dummy base, not a bare marker, because
        disabled tools still call self._tool_error and pyright checks them.
        """
        return make_tool_error(message, code=code, **details)


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
# Sync tools that declare timeout= cannot enforce it. Warn once per name.
_sync_timeout_warned: set[str] = set()
_UNSET_EXCLUDE_TIERS = object()

def tool_supports_document(tool: ToolBase, *, doc_type: str | None, uno_services_supported: frozenset[str] | None) -> bool:
    """Return True when *tool* is allowed on a document with the cached type/services."""
    if tool.uno_services is None and tool.doc_types is None:
        return True

    services = uno_services_supported
    if not services and doc_type:
        from plugin.doc.doc_type import uno_services_for_doc_type_label
        services = uno_services_for_doc_type_label(doc_type)

    if tool.uno_services is not None and services:
        if any(svc in services for svc in tool.uno_services):
            return True

    if tool.doc_types is not None:
        if doc_type and doc_type.lower() in {str(d).lower() for d in tool.doc_types}:
            return True
        if tool.uno_services is None:
            return False

    return False


class ToolRegistry:
    """Registers and dispatches tools.

    Both the chatbot and MCP server use this single registry.
    """

    _services: Any
    batch_mode: bool

    def __init__(self, services: Any) -> None:
        self._services = services
        self._tools: dict[str, ToolBase] = {}  # name -> ToolBase instance
        # Shared names (shape_upsert / manage_charts) last-wins the instance but
        # must keep the union of required_core_tools. ClassVar cannot be set on
        # the instance, so store the merge here.
        self._required_core_union: dict[str, frozenset[str]] = {}
        self.batch_mode = False  # suppress per-tool cache invalidation

    # ── Registration ──────────────────────────────────────────────────

    def register(self, tool: ToolBase) -> None:
        """Register a single ToolBase instance."""
        # crosshair: off
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
            # Same class object (re-import of the same module): skip silently.
            # Same __name__ from a *different* module is last-wins (Writer/Calc/Draw
            # wrappers for shape_upsert / manage_charts) — log so registration order is visible.
            if type(existing_tool).__name__ != type(tool).__name__ or type(existing_tool).__module__ != type(tool).__module__:
                log.warning("Tool '%s' already registered (class %s from %s), replacing with class %s from %s", tool.name, type(existing_tool).__name__, type(existing_tool).__module__, type(tool).__name__, type(tool).__module__)
            # Shared names replace the instance (Writer loads last). Union both
            # required_core_tools sets, plus any prior merge, or Calc
            # shapes/charts never request get_sheet_summary / read_cell_range.
            # get_tools still drops cores that fail tool_supports_document
            # for the active doc.
            prev = self._required_core_union.get(tool.name) or getattr(existing_tool, "required_core_tools", None)
            nxt = getattr(tool, "required_core_tools", None)
            if prev or nxt:
                self._required_core_union[tool.name] = frozenset(prev or ()) | frozenset(nxt or ())
        self._tools[tool.name] = tool

    def auto_discover_package(self, package_name: str) -> None:
        """Automatically discover and register ToolBase subclasses in all submodules of a package."""
        # crosshair: off
        import importlib
        import pkgutil

        # Import the package itself to get its path
        package = importlib.import_module(package_name)

        # Iterate over all submodules in the package directory
        for _unused, module_name, _is_pkg in pkgutil.iter_modules(package.__path__):
            full_module_name = f"{package_name}.{module_name}"
            try:
                module = importlib.import_module(full_module_name)
                self.auto_discover(module)
            except Exception:
                log.exception("Failed to import module %s for tool discovery", full_module_name)

    def auto_discover(self, module: Any) -> None:
        """Automatically discover and register ToolBase subclasses in a module."""
        # crosshair: off
        # ToolBaseDummy disables a tool without deleting it. Defined-in-module
        # avoids double registration from imports. Abstract classes and classes
        # without ``name`` are not tools.
        from plugin.framework.service import iter_named_concrete_subclasses

        for obj in iter_named_concrete_subclasses(module, ToolBase, ToolBaseDummy):
            try:
                tool_instance = obj()
                self.register(tool_instance)
            except Exception:
                log.exception("Failed to instantiate tool %s", obj.__name__)

    # ── Lookup & Schema Generation ────────────────────────────────────

    def get_tools(
        self, doc: Any = None, doc_type: str | None = None, tier: str | None = None, intent: str | None = None, names: Any = None, filter_doc_type: bool = True, exclude_tiers: Any = _UNSET_EXCLUDE_TIERS, active_domain: str | None = None, uno_services_supported: Any = None, **kwargs: Any
    ) -> list[ToolBase]:
        """Return a list of ToolBase instances matching the given criteria.

        Args:
            doc: Optional document model (legacy; prefer doc_type + uno_services_supported).
            doc_type: Optional string indicating compatibility ("writer", "calc", "draw").
            uno_services_supported: Cached UNO service names from sidebar/MCP (no live doc probe).
            tier: Optional string; main chat tools use ``"core"``.
            intent: Optional string filtering by tool intent.
            names: Optional collection of exact tool names. ``None`` applies
                no name filter. An empty collection matches nothing. A bare
                string is that one name, not a substring.
            filter_doc_type: If True, filters by doc model services or doc_type. Defaults to True.
            exclude_tiers: Tiers to omit from the result. If omitted, excludes
                ``specialized``, ``specialized_control``, and ``mcp`` so nested
                Writer tools and MCP-only helpers stay off the main tool list.
                Pass ``()`` or ``frozenset()`` to include all tiers.
            active_domain: If provided, dynamically includes specialized tools for this domain
                and the specialized_workflow_finished tool.
        """
        # crosshair: off
        tools: Any = self._tools.values()

        if doc_type is None and doc is not None and filter_doc_type:
            doc_type = _doc_type_str_from_doc(doc)
        if uno_services_supported is None and doc is not None and filter_doc_type:
            from plugin.framework.thread_guard import on_main_thread
            from plugin.doc.doc_type import get_document_uno_services

            if on_main_thread():
                uno_services_supported = get_document_uno_services(doc)

        # Helper to check if a tool supports the document
        def supports_doc(t: Any) -> bool:
            if not filter_doc_type:
                return True
            return tool_supports_document(t, doc_type=doc_type, uno_services_supported=uno_services_supported)

        tools = [t for t in tools if supports_doc(t)]

        # If we have an active domain, we want to include its tools (and the finish tool),
        # even if they are in the excluded tiers.

        if exclude_tiers is _UNSET_EXCLUDE_TIERS:
            to_exclude = _DEFAULT_EXCLUDE_TIERS
        else:
            to_exclude = frozenset(exclude_tiers) if exclude_tiers else frozenset()

        if active_domain:
            # If an active domain is set, restrict the list ONLY to the specialized tools
            # for that domain and the finish tool. Do not include normal default-tier tools.
            # However, we also include any core tools explicitly requested by the domain.

            # First, find which core tools are required by any tool in this domain
            required_core: set[str] = set()
            for t in tools:
                if _is_specialized_domain_tool(t, active_domain):
                    req = (self._required_core_union.get(t.name) if t.name else None) or getattr(t, "required_core_tools", None)
                    if req:
                        required_core.update(req)

            from plugin.framework.prompts import WRITER_SIDEBAR_ONLY_DOMAINS

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

                def _tier_excluded(t: Any) -> bool:
                    tier = getattr(t, "tier", None)
                    return tier in to_exclude

                tools = [t for t in tools if not _tier_excluded(t)]

        if tier:
            tools = [t for t in tools if t.tier == tier]
        if intent:
            tools = [t for t in tools if t.intent == intent]
        # Only None skips the filter. Callers pass tool names, not a substring
        # to search. ``if names`` treats ``[]`` and ``""`` as no filter and
        # returns every tool. A bare string is iterable, so ``t.name in
        # "target"`` matches the tool named ``get``. A str is one exact name;
        # any other collection becomes a set, and an empty set matches nothing.
        if names is not None:
            if isinstance(names, str):
                name_set = frozenset((names,))
            else:
                name_set = frozenset(names)
            tools = [t for t in tools if t.name in name_set]
        ctx = kwargs.get("ctx")
        if ctx is not None:
            from plugin.vision.vision_availability import filter_vision_specialized_tools

            tools = filter_vision_specialized_tools(list(tools), ctx)
        return list(tools)

    def get_schemas(self, protocol: str = "openai", active_domain: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        """Return schemas for tools matching the given kwargs criteria.

        Args:
            protocol: Either "openai" or "mcp".
            active_domain: Optional active specialized domain.
            **kwargs: Filters passed to get_tools().
        """
        # crosshair: off
        tools = self.get_tools(active_domain=active_domain, **kwargs)
        doc_type = kwargs.get("doc_type") or _doc_type_str_from_doc(kwargs.get("doc"))
        if protocol == "openai":
            # get_image is only useful to a vision-capable text model on the chat path; hide it from
            # text-only models. The MCP path (below) always keeps it (client assumed vision-capable).
            from plugin.vision.image_filter import filter_get_image_for_text_only_model

            tools = filter_get_image_for_text_only_model(tools)
            schemas = [to_openai_schema(t, doc_type=doc_type) for t in tools]
            ctx = kwargs.get("ctx")
            if ctx is not None:
                from plugin.vision.vision_availability import filter_vision_delegate_schemas

                schemas = filter_vision_delegate_schemas(schemas, ctx)
            from plugin.doc.peer_message import filter_peer_message_schemas

            schemas = filter_peer_message_schemas(schemas, ctx, doc=kwargs.get("doc"), active_domain=active_domain)
            return schemas
        elif protocol == "mcp":
            # Never advertise peer send tools on MCP, including find_tools(domain=…).
            from plugin.doc.peer_message import filter_peer_message_schemas

            schemas = [to_mcp_schema(t, doc_type=doc_type) for t in tools]
            return filter_peer_message_schemas(schemas, kwargs.get("ctx"), doc=kwargs.get("doc"), active_domain=None)
        else:
            raise ValueError(f"Unknown protocol: {protocol}")

    def get(self, name: str) -> ToolBase | None:
        """Get a tool by name, or None."""
        return self._tools.get(name)

    # ── Execution ─────────────────────────────────────────────────────

    def _get_tool_timeout(self, tool: ToolBase) -> float:
        return getattr(tool, "timeout", 0)

    def _execute_with_timeout(self, func: Any, timeout: float, tool_name: str = "<unknown>", run_threaded: bool = True, **kwargs: Any) -> Any:
        """Run *func* with an optional wall-clock timeout.

        If ``run_threaded`` is False, the timeout is ignored and the function runs inline.
        Sync tools are marshaled to the main thread by ``ToolRegistry.execute`` before this runs.

        Timeout **abandons** the worker; it does not kill the dedicated thread.
        The thread may run to completion with its result dropped when nothing
        was queued yet. A result already queued is kept. This method does not
        cancel the send: the caller still has to deliver the ``TOOL_TIMEOUT``
        dict (the drain turns that into ``TOOL_DONE``).
        """
        # crosshair: off
        if timeout <= 0:
            return func(**kwargs)

        if not run_threaded:
            # The timeout needs a thread, and execute hits this path on each
            # run. One line per tool name is enough; warning every call
            # floods the log for a sync tool with timeout=.
            if tool_name not in _sync_timeout_warned:
                _sync_timeout_warned.add(tool_name)
                log.warning("Tool '%s' declares timeout=%s but is synchronous; timeout is ignored. Set is_async() to True to enable timeout enforcement.", tool_name, timeout)
            return func(**kwargs)

        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        def worker() -> None:
            try:
                result_queue.put(("success", func(**kwargs)))
            except Exception as e:
                result_queue.put(("error", e))
            except BaseException as exc:
                # Queue the error before unwinding. ``except Exception`` leaves
                # KeyboardInterrupt, SystemExit, and GeneratorExit off the
                # queue. ``run_in_background`` lets BaseException unwind, the
                # dedicated thread dies, ``join`` returns, ``is_alive()`` is
                # false, and ``result_queue.get()`` blocks the send forever.
                result_queue.put(("error", exc))
                log.exception("Tool '%s' worker exited", tool_name)
                raise

        worker_thread = run_in_background(worker, name=f"tool-timeout-{tool_name}", dedicated=True)
        worker_thread.join(timeout=timeout)

        def _queued() -> tuple[str, Any] | None:
            try:
                return result_queue.get_nowait()
            except queue.Empty:
                return None

        # A queued result wins. The worker can ``put`` a success and still be
        # alive while ``run_in_background`` logs and clears the thread-guard
        # tag. Treating timeout as only ``is_alive()`` after ``join`` reports
        # that finished mutation as TOOL_TIMEOUT, and a retry can apply it
        # again. A live thread with an empty queue is still a timeout — the
        # wait is not extended.
        if worker_thread.is_alive():
            queued = _queued()
            if queued is None:
                # The worker is already abandoned. Return the timeout dict and let
                # the caller decide whether the send should stop. Calling
                # ``send_cancellation.cancel()`` as well sets the drain stop
                # checker, which treats the send as Stop and discards the
                # error dict before ``TOOL_DONE`` is applied. The tool loop
                # then waits forever for that event.
                return make_tool_error(f"Tool timed out after {timeout} seconds", code="TOOL_TIMEOUT", tool_name=tool_name)
        else:
            queued = _queued()
            if queued is None:
                # Dead with nothing queued (the put itself failed, or the
                # thread exited before the handler ran). Do not block on get().
                return make_tool_error(
                    f"Tool '{tool_name}' worker exited without a result",
                    code="TOOL_WORKER_EXIT",
                    tool_name=tool_name,
                )

        result_type, result = queued
        if result_type == "error":
            if not isinstance(result, Exception):
                # Re-raising BaseException would skip the send worker's
                # ``except Exception`` and leave the turn waiting for TOOL_DONE.
                return make_tool_error(
                    f"Tool '{tool_name}' worker exited: {type(result).__name__}: {result}",
                    code="TOOL_WORKER_EXIT",
                    tool_name=tool_name,
                    error_type=type(result).__name__,
                )
            raise result  # Will be caught by outer try/except

        return result

    def execute(self, tool_name: str, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Execute a tool by name.

        Args:
            tool_name: Registered tool name.
            ctx:       ToolContext for this invocation.
            **kwargs:  Tool arguments.

        Returns:
            dict: Result from the tool execution (status ``ok`` or ``error``).
        """
        # crosshair: off
        # A model could send an argument named bypass_thread_guard. Drop it so
        # only execute_unguarded() can skip the main-thread marshal.
        kwargs.pop("bypass_thread_guard", None)
        return self._execute_impl(tool_name, ctx, False, **kwargs)

    def execute_unguarded(self, tool_name: str, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Execute a tool by name, bypassing the main-thread check.

        Used only by ``scripts/prompt_optimization/tools_lo`` where UNO runs on a dedicated
        LibreOffice worker thread (not Python's ``main_thread()``).
        """
        # crosshair: off
        return self._execute_impl(tool_name, ctx, True, **kwargs)

    def _execute_impl(self, tool_name: str, ctx: ToolContext, bypass_thread_guard: bool, **kwargs: Any) -> dict[str, Any]:
        try:
            tool = self._tools.get(tool_name)
            if tool is None:
                # Return a structured error instead of raising KeyError so the model
                # receives actionable feedback ("no tool named X") rather than an
                # opaque traceback via execute_fn's unexpected-exception handler.
                # This covers hallucinated names (e.g. 'write_formulas') and the
                # 'unknown' sentinel from null tool-name tool calls alike.
                return make_tool_error(f"No tool named '{tool_name}' is registered. Check the tool name and retry.", code="UNKNOWN_TOOL", tool_name=tool_name)

            # Check document compatibility using cached doc_type / UNO services (no live doc probe).
            if not tool_supports_document(tool, doc_type=ctx.doc_type, uno_services_supported=getattr(ctx, "uno_services_supported", None)):
                # A registered tool whose uno_services/doc_types miss this document
                # used to raise ValueError, and ``except ValueError`` re-raised it
                # into the model as a traceback. Unknown names stay UNKNOWN_TOOL.
                # This path is a stable tool error the model can read.
                return make_tool_error(
                    f"Tool '{tool_name}' does not support the current document.",
                    code="UNSUPPORTED_DOCUMENT",
                    tool_name=tool_name,
                )

            # Restrict kwargs to this tool's schema so extra keys (e.g. image_model
            # from API/LLM) do not cause "Unknown parameter" validation errors.
            # scripting_only_parameters (e.g. set_style number_format) are only kept
            # for scripting callers — chat/MCP models must not apply hidden parameters
            # from training memory when the property was removed from the schema
            # (see docs/calc/date-time-handling.md S26).
            schema = tool.get_parameters(ctx.doc_type) or {}
            props = call_properties(schema) or {}
            extra_ok = (getattr(tool, "scripting_only_parameters", None) or frozenset()) if ctx.caller == "script" else frozenset()
            # One allow-list (including properties {}). The outbound check
            # uses the same helper before any provider shim.
            kept = without_unknown_kwargs(schema, kwargs, extra_ok)
            # Keep the names that were dropped: they are reported back as ignored_parameters.
            ignored: list[str] = sorted(k for k in kwargs if k not in kept)
            kwargs = kept

            required = schema.get("required") or []
            required_names = set(required) if isinstance(required, list) else set()
            if any(v is None and k not in required_names for k, v in kwargs.items()):
                # Advertised schemas allow JSON null on optional scalars. Models
                # send null for an omitted optional, and validate() uses the
                # source schema, which rejects None. Drop those keys so the
                # tool sees the argument as omitted. A required null still
                # fails validation.
                kwargs = {k: v for k, v in kwargs.items() if v is not None or k in required_names}

            kwargs = coerce_call_args(tool.name, props, kwargs)

            # Common context for all error details
            common_details = {"tool_name": tool_name}
            if ctx.caller:
                common_details["caller"] = ctx.caller
            if ctx.doc_type:
                common_details["doc_type"] = ctx.doc_type

            # Validate parameters
            ok, err = tool.validate(doc_type=ctx.doc_type, **kwargs)
            if not ok:
                # "Missing required parameter: index" is exactly when the agent sent the value
                # under another name (paragraph_index): show which names were dropped.
                extra = {"ignored_parameters": ignored} if ignored else {}
                return make_tool_error(err, code="VALIDATION_ERROR", **common_details, **extra)

            if getattr(ctx, "read_only_target", False) and tool.detects_mutation():
                # Use the central factory (all tool errors now go through make_tool_error).
                return make_tool_error("This document is open for read-only document_research access; writes are not allowed.", code="READ_ONLY_TARGET", **common_details)

            # Execution with simple isolation and timeout.
            # Async tools (and bypass_thread_guard eval paths) run on the caller's thread.
            # Sync tools are marshaled to the LO main thread so MCP long-running and any
            # other worker-thread caller cannot touch UNO off-thread.
            runner = tool.execute if bypass_thread_guard else tool.execute_safe
            run_threaded = bypass_thread_guard or bool(tool.is_async())
            timeout = self._get_tool_timeout(tool)

            def _invoke() -> Any:
                return self._execute_with_timeout(runner, timeout=timeout, tool_name=tool_name, run_threaded=run_threaded, ctx=ctx, **kwargs)

            if bypass_thread_guard or tool.is_async():
                result = _invoke()
            else:
                # We let the marshal TimeoutError become TOOL_REGISTRY_ERROR, not a
                # TOOL_TIMEOUT/cancel path, because QueueExecutor raises it only for
                # work that never started (started work is waited out), so nothing
                # lands late and a retry is safe.
                result = execute_on_main_thread(_invoke)

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

            # The dropped keys used to vanish silently: image_insert(paragraph_index=70) -- the
            # name image_list reports -- dropped the index and put the image at the view cursor
            # with status ok (relato #25). Say which arguments were not used.
            if ignored and isinstance(result, dict):
                cast("dict[str, Any]", result).setdefault("ignored_parameters", ignored)

            return result

        except Exception as e:
            log.exception("Tool execution failed: %s", tool_name)
            return make_tool_error(f"Failed to execute tool '{tool_name}'", code="TOOL_REGISTRY_ERROR", tool_name=tool_name, error=str(e), type=type(e).__name__)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
