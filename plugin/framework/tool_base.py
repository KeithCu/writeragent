# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""Base class for all tools."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from plugin.framework.errors import ToolExecutionError, format_error_payload

_log = logging.getLogger(__name__)


_READ_PREFIXES = ("get_", "read_", "list_", "find_", "search_", "count_")


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
    def execute(self, ctx, **kwargs) -> dict[str, Any]:
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

    def execute_safe(self, ctx, **kwargs) -> dict[str, Any]:
        """Execute with simple error containment."""
        try:
            # Check thread safety: If the tool is synchronous, it must not be called from a background thread
            # unless it's being executed through the QueueExecutor. Direct UNO calls from background threads
            # cause UI hangs and deadlocks in LibreOffice.
            if not self.is_async():
                import threading
                if threading.current_thread() is not threading.main_thread():
                    raise RuntimeError(
                        f"Thread Safety Violation: Synchronous tool '{self.name}' was executed from a background thread. "
                        "Synchronous tools execute UNO APIs which are not thread-safe. You must wrap this call "
                        "using `execute_on_main_thread` from `plugin.framework.queue_executor`."
                    )
            return self.execute(ctx, **kwargs)
        except Exception as e:
            _log.exception(
                "Tool '%s' execution failed",
                self.name if self.name else "<unknown>",
            )
            return self._tool_error(
                f"Tool execution failed: {str(e)}",
                code="TOOL_EXECUTION_ERROR",
                original_error=str(e),
                error_type=type(e).__name__
            )

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
            return self._tool_error(
                msg,
                code="UNO_OBJECT_ERROR",
                item_name=item_name,
                getter_name=getter_name,
                available=available
            )

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
            return self._tool_error(
                msg,
                code="UNO_OBJECT_ERROR",
                item_name=item_name,
                getter_name=getter_name,
                available=available
            )
        return collection.getByName(item_name)

