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
"""Per-document mutation gate for MCP tools/call.

Serializes mutating MCP runs that share a normalized document key.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.framework.errors import WriterAgentException

log = logging.getLogger("writeragent.mcp.protocol")

_doc_gates: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_doc_gates_guard = threading.Lock()


def _get_document_mutation_gate(doc_key: str) -> threading.Lock:
    # Callers hold a strong local reference for the whole acquire/release;
    # a gate no caller holds may be collected.
    with _doc_gates_guard:
        gate = _doc_gates.get(doc_key)
        if gate is None:
            gate = threading.Lock()
            _doc_gates[doc_key] = gate
        return gate


def _tool_needs_document_mutation_gate(tool: Any, arguments: Any = None) -> bool:  # pyright: ignore[reportUnusedFunction]
    if tool is None:
        return True  # unknown tool -> be safe
    # A broken requires_document_lock must not silently switch to
    # detects_mutation. ToolBase.requires_document_lock already calls that.
    # An override that raises is a bug and must surface.
    return bool(tool.requires_document_lock(arguments))


@contextmanager
def _document_mutation_gate(doc_key: str, *, enabled: bool, timeout: float = 30.0) -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
    if not enabled:
        yield
        return
    gate = _get_document_mutation_gate(doc_key)
    started = time.monotonic()
    acquired = gate.acquire(timeout=timeout)
    if not acquired:
        log.warning("MCP _document_mutation_gate timed out after %.2fs waiting for %s", time.monotonic() - started, doc_key)
        raise BusyError("Timed out waiting for another edit to this document. Retry in a moment.")
    try:
        yield
    finally:
        gate.release()


class BusyError(WriterAgentException):
    """A tool run is already in progress (global or per-document lock)."""

    code: str = "SERVER_BUSY"
