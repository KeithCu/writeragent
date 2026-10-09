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
"""Base class for ACP (Agent Communication Protocol) backends.

Extracts common ACP logic: connection management, session handling,
notification processing, and prompt formatting.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import threading
import time
from typing import Any, Optional, Dict, List, Tuple

from plugin.acp.base import AgentBackend
from plugin.acp.acp_connection import ACPConnection
from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.errors import format_error_payload
from plugin.version import EXTENSION_VERSION

log = logging.getLogger(__name__)

# ACP protocol version (integer per SDK)
_ACP_PROTOCOL_VERSION = 1
# After spawn, wait this long before initialize so a process that exits
# immediately is reported as a start failure. The wait is polled so Stop
# during it does not continue into the handshake.
_STARTUP_WAIT_S = 0.5
_STARTUP_POLL_S = 0.05
# Slice count for the grace period. Not ``int(wait / poll)``: 0.05 is not
# an exact binary fraction, and truncating a 9.999 result would shorten it.
_STARTUP_POLLS = round(_STARTUP_WAIT_S / _STARTUP_POLL_S)

# PermissionOption.kind values from the ACP schema. Prefer the "_once"
# choice so Approve does not permanently allow later tool calls.
_ALLOW_KINDS = ("allow_once", "allow_always")
_REJECT_KINDS = ("reject_once", "reject_always")
# tool_call_update statuses that carry a finished invocation. in_progress
# is still tool activity, but it is not a result yet.
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})
# shutil.which on Windows returns the PATHEXT hit (hermes.exe, hermes.cmd,
# hermes.bat). Those stems are the same CLI as the POSIX basename.
_WINDOWS_CLI_SUFFIXES = frozenset({".exe", ".cmd", ".bat"})


def _cli_basename_matches(path: str, binary_name: str) -> bool:
    """True when ``path`` is this CLI, including Windows launcher suffixes.

    ``shutil.which`` on Windows returns ``hermes.exe``, ``hermes.cmd``, or
    ``hermes.bat``. Comparing that basename to ``get_binary_name()``
    (``hermes``) fails, so ``default_extra_args`` (``("acp",)``) is never
    appended and the CLI starts interactive instead of ACP stdio. Match the
    stem when the suffix is one of those three, case-insensitively. Any other
    suffix stays an exact basename compare so a wrapper is not treated as
    the official binary.
    """
    name = os.path.basename(path).lower()
    expected = binary_name.lower()
    if name == expected:
        return True
    stem, ext = os.path.splitext(name)
    return stem == expected and ext in _WINDOWS_CLI_SUFFIXES


def _cancelled_permission() -> dict[str, Any]:
    return {"outcome": {"outcome": "cancelled"}}


def _permission_description(params: dict[str, Any]) -> str:
    """Dialog text from the ACP fields that actually carry it.

    ACP puts the human text on ``toolCall.title`` and ``options[].name``.
    ``params["description"]`` is a private key, so a conformant request
    showed only the generic fallback. Build the dialog string from those
    fields and ignore the private key.
    """
    tool_call = params.get("toolCall")
    title = ""
    if isinstance(tool_call, dict):
        title = str(tool_call.get("title") or "").strip()
    names: list[str] = []
    options = params.get("options")
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            name = str(opt.get("name") or "").strip()
            if name:
                names.append(name)
    parts = [part for part in (title, *names) if part]
    if parts:
        return "\n".join(parts)
    return "Agent requests permission"


def _permission_tool_name(tool_call: dict[str, Any]) -> str:
    kind = str(tool_call.get("kind") or "").strip()
    if kind and kind != "other":
        return kind
    return str(tool_call.get("toolCallId") or "").strip()


def _option_id_for_decision(options: list[Any], approved: bool) -> str | None:
    """Echo an ``optionId`` the agent offered for this Approve/Reject click.

    Approve must not echo a ``reject_*`` id: the agent would treat that as a
    denial. An unlabeled option is only used for Approve. Reject with no
    ``reject_*`` option returns None so the caller sends ``cancelled``.
    """
    preferred = _ALLOW_KINDS if approved else _REJECT_KINDS
    by_kind: dict[str, str] = {}
    unlabeled: str | None = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        option_id = opt.get("optionId")
        if not isinstance(option_id, str) or not option_id:
            continue
        kind = opt.get("kind")
        if isinstance(kind, str) and kind:
            if kind not in by_kind:
                by_kind[kind] = option_id
        elif unlabeled is None:
            unlabeled = option_id
    for kind in preferred:
        found = by_kind.get(kind)
        if found:
            return found
    if approved:
        return unlabeled
    return None


def _permission_result(options: list[Any], approved: bool) -> dict[str, Any]:
    """ACP ``session/request_permission`` result (``selected`` or ``cancelled``)."""
    option_id = _option_id_for_decision(options, approved)
    if option_id is None:
        if approved:
            log.warning("ACP permission approve had no optionId; sending cancelled")
        return _cancelled_permission()
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _queue_text_blocks(content: Any, queue: Any, kind: StreamQueueKind) -> None:
    if isinstance(content, dict):
        blocks: list[Any] = [content]
    elif isinstance(content, list):
        blocks = content
    else:
        return
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if isinstance(text, str):
            queue.put((kind, text))


def _tool_activity_payload(update: dict[str, Any]) -> dict[str, Any]:
    session_update = update.get("sessionUpdate")
    payload: dict[str, Any] = {"type": "tool_call" if session_update == "tool_call" else "tool_call_update"}
    for src, dest in (("toolCallId", "id"), ("title", "title"), ("kind", "kind"), ("status", "status"), ("rawInput", "rawInput"), ("rawOutput", "rawOutput"), ("content", "content"), ("locations", "locations")):
        if src in update and update[src] is not None:
            payload[dest] = update[src]
    return payload


def _tool_stream_kind(update: dict[str, Any]) -> StreamQueueKind:
    if update.get("sessionUpdate") != "tool_call_update":
        return StreamQueueKind.TOOL_CALL
    status = update.get("status")
    if status in _TERMINAL_TOOL_STATUSES or "content" in update or "rawOutput" in update:
        return StreamQueueKind.TOOL_RESULT
    return StreamQueueKind.TOOL_CALL


class ACPBackend(AgentBackend):
    """Base class for ACP-based agent backends.

    Subclasses must implement:
    - get_binary_name(): return binary name (e.g., "hermes")
    - get_display_name(): return UI display name
    - get_agent_name(): return ACP agent name
    - get_env_vars(): return dict of environment variables to pass

    Optional class attr for CLIs that need a default subcommand when
    ``agent_backend.args`` is empty (Hermes/OpenCode ``acp``, Grok
    ``--no-auto-update agent stdio``):
    - default_extra_args: immutable tuple; ``get_default_extra_args()``
      copies it onto ``_extra_args`` when the resolved basename equals
      ``get_binary_name()``, or that name plus ``.exe`` / ``.cmd`` / ``.bat``.
      Grok overrides ``_apply_default_extra_args`` for prefix matching.
    """

    default_extra_args: Tuple[str, ...] = ()
    _ctx: Any | None
    _stop_requested: bool
    _prompt_done: threading.Event
    _permission_lock: threading.Lock
    _pending_permissions: dict[Any, list[Any]]

    def __init__(self, ctx: Any | None = None) -> None:
        self._ctx = ctx
        self._conn: ACPConnection | None = None
        self._session_id: str | None = None
        self._stop_requested = False
        self._binary_path: Optional[str] = None
        self._extra_args: List[str] = []
        self._prompt_done = threading.Event()
        # request id -> options from session/request_permission. Stop and
        # submit_approval both answer these; the lock is the only guard.
        self._permission_lock = threading.Lock()
        self._pending_permissions = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from WriterAgent settings."""
        try:
            from plugin.framework.config import get_config

            path = str(get_config("agent_backend.path") or "").strip()
            if path and os.path.isfile(path):
                self._binary_path = path
            else:
                self._binary_path = self._find_binary()

            args_str = str(get_config("agent_backend.args") or "").strip()
            # str.split() breaks a quoted path into several argv entries.
            # posix=False on Windows so backslashes in paths stay literal.
            self._extra_args = shlex.split(args_str, posix=(os.name != "nt")) if args_str else []
        except Exception:
            # A failed reload used to keep the previous argv, and the
            # exception was swallowed so a broken config looked like a
            # missing binary.
            log.exception("ACP agent config load failed")
            self._binary_path = self._find_binary()
            self._extra_args = []
        self._apply_default_extra_args()

    def get_default_extra_args(self) -> List[str]:
        """CLI args used when settings args are empty and the binary matches this backend."""
        return list(self.default_extra_args)

    def _apply_default_extra_args(self) -> None:
        """Fill ``_extra_args`` from ``get_default_extra_args()`` when settings left them empty."""
        if self._extra_args:
            return
        defaults = self.get_default_extra_args()
        if not defaults or not self._binary_path:
            return
        if _cli_basename_matches(self._binary_path, self.get_binary_name()):
            self._extra_args = list(defaults)

    def _find_binary(self) -> str | None:
        """Find the binary in PATH or common locations."""
        binary_name = self.get_binary_name()

        # Try the binary name directly
        path = shutil.which(binary_name)
        if path:
            return path

        # Check common install locations
        home = os.path.expanduser("~")
        for candidate in (os.path.join(home, ".local", "bin", binary_name), os.path.join(home, ".cargo", "bin", binary_name)):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return None

    def get_binary_name(self) -> str:
        """Return the binary name to search for (e.g., 'hermes')."""
        raise NotImplementedError

    def get_display_name(self) -> str:
        """Return display name for UI."""
        raise NotImplementedError

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        raise NotImplementedError

    def get_env_vars(self) -> Dict[str, str]:
        """Return environment variables to pass to subprocess."""
        return {}

    def is_available(self, ctx: Any) -> bool:
        """Check if binary is installed."""
        self._load_config()
        if self._binary_path and os.path.isfile(self._binary_path):
            log.info(f"{self.get_display_name()} binary found: {self._binary_path}")
            return True
        # Fallback: search PATH
        binary_name = self.get_binary_name()
        path = shutil.which(binary_name)
        if path:
            self._binary_path = path
            self._apply_default_extra_args()
            log.info(f"{self.get_display_name()} found via PATH: {path}")
            return True
        log.info(f"{self.get_display_name()} binary not found")
        return False

    def _startup_stopped(self, stop_checker: Any) -> bool:
        """True when the post-start wait must leave before initialize.

        A true ``stop_checker`` latches ``_stop_requested`` so ``send()``
        takes the same ``_finish_stopped`` path it uses when Stop wins
        before the prompt.
        """
        if self._stop_requested:
            return True
        if callable(stop_checker) and bool(stop_checker()):
            self._stop_requested = True
            return True
        return False

    def _ensure_connection(self, stop_checker: Any = None) -> None:
        """Start the ACP subprocess if not already running."""
        if self._conn and self._conn.is_alive:
            return
        if not self._binary_path:
            raise RuntimeError(f"{self.get_display_name()} binary not found. Install {self.get_binary_name()} and ensure it's in PATH.")

        cmd_line = [self._binary_path]
        cmd_line.extend(self._extra_args)

        env = dict(os.environ)
        env.update(self.get_env_vars())

        conn = ACPConnection(cmd_line=cmd_line, env=env)
        self._conn = conn
        conn.start()

        # time.sleep(0.5) after start() never looked at Stop. stop() or a
        # true stop_checker during that half-second was ignored, and this
        # method continued into initialize. Poll the same grace period in
        # short slices and return as soon as the stop latch or stop_checker()
        # says so, before the handshake.
        # Keep the connection created above. stop() sets self._conn to None,
        # so re-reading it for the handshake raises AttributeError.
        polls_left = _STARTUP_POLLS
        while True:
            if self._startup_stopped(stop_checker):
                return
            if not conn.is_alive:
                if self._startup_stopped(stop_checker):
                    return
                detail = conn.stderr_text().strip()
                message = f"{self.get_display_name()} ACP process failed to start."
                if detail:
                    message = f"{message} {detail[:500]}"
                raise RuntimeError(message)
            if polls_left <= 0:
                break
            polls_left -= 1
            time.sleep(_STARTUP_POLL_S)

        # Initialize handshake. ACP fs capability names are camelCase;
        # snake_case keys are ignored and do not turn the features off.
        try:
            result = conn.send_request(
                "initialize",
                {
                    "protocolVersion": _ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "WriterAgent", "version": EXTENSION_VERSION},
                },
                timeout=15,
            )
            log.info(f"ACP initialized: {result}")
        except Exception:
            log.exception("ACP initialize failed")
            try:
                conn.stop()
            finally:
                if self._conn is conn:
                    self._conn = None
            raise

    def _ensure_session(self, mcp_url: str | None = None) -> None:
        """Create a new ACP session if needed."""
        if self._session_id:
            return

        # mcp_servers is required by the ACP schema
        # Attach mcp_servers only when MCP is enabled. A disabled server must
        # not be handed to session/new.
        from plugin.framework.config import get_config
        from plugin.framework.config_schema import as_bool

        mcp_servers = []
        if mcp_url and as_bool(get_config("mcp.mcp_enabled")):
            mcp_servers.append({"url": mcp_url, "name": "writeragent", "type": "http", "headers": []})

        params = {"cwd": os.getcwd(), "mcpServers": mcp_servers}

        try:
            if self._conn:
                result = self._conn.send_request("session/new", params, timeout=30)
                session_id = result.get("sessionId") if isinstance(result, dict) else None
                # An empty id used to be stored and sent on session/prompt.
                if not isinstance(session_id, str) or not session_id.strip():
                    raise RuntimeError(f"{self.get_display_name()} ACP session/new returned no sessionId.")
                self._session_id = session_id
                log.debug(f"ACP session created: {self._session_id}")
        except Exception:
            log.exception("ACP session creation failed")
            raise

    def _build_prompt_blocks(self, user_message: str, document_context: Optional[str] = None, system_prompt: Optional[str] = None, selection_text: Optional[str] = None, document_url: Optional[str] = None) -> list[dict[str, Any]]:
        """Build ACP prompt content blocks."""
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")

        if is_slash_command:
            # For slash commands, only send the command itself
            prompt_blocks.append({"type": "text", "text": user_message})
        else:
            # Add system prompt if provided
            if system_prompt:
                prompt_blocks.append({"type": "text", "text": system_prompt})
            # Add document context if provided
            if document_context:
                prompt_blocks.append({"type": "text", "text": f"[DOCUMENT CONTENT]\n{document_context}"})
            # Add selection text if provided
            if selection_text:
                prompt_blocks.append({"type": "text", "text": f"[SELECTED TEXT]\n{selection_text}"})
            # Add document URL if provided
            if document_url:
                prompt_blocks.append({"type": "text", "text": f"Document URL: {document_url}"})
            # Always add the user message last
            prompt_blocks.append({"type": "text", "text": user_message})

        return prompt_blocks

    def _handle_acp_update(self, update: Any, queue: Any) -> None:
        """Queue transcript events from an ACP ``session/update`` or a legacy content list.

        Spec payloads set ``sessionUpdate``. ``agent_message_chunk`` is the
        answer (CHUNK). ``agent_thought_chunk`` is reasoning (THINKING): the
        send drain persists only non-thinking chunks, so thoughts must not
        use CHUNK or they are saved as the assistant answer. ``tool_call`` /
        ``tool_call_update`` are the update object itself; they are not
        ``content`` blocks with ``type: tool_call``.

        Prompt results (Vibe ``contentBlocks``) and older private notifications
        still pass a ``content`` list of ``text`` / ``tool_call`` / ``tool_result``
        blocks. That path stays for those producers.
        """
        if not isinstance(update, dict):
            return
        session_update = update.get("sessionUpdate")
        if isinstance(session_update, str) and session_update:
            self._handle_session_update(update, session_update, queue)
            return
        self._handle_legacy_content(update, queue)

    def _handle_session_update(self, update: dict[str, Any], session_update: str, queue: Any) -> None:
        if session_update == "agent_message_chunk":
            _queue_text_blocks(update.get("content"), queue, StreamQueueKind.CHUNK)
        elif session_update == "agent_thought_chunk":
            # Thought text lives in ``content`` with ``type: text``. The legacy
            # path queued that as CHUNK, and the agent drain appends every
            # non-thinking chunk into the saved assistant row. THINKING is
            # displayed when thinking is on and is left out of that row.
            _queue_text_blocks(update.get("content"), queue, StreamQueueKind.THINKING)
        elif session_update in ("tool_call", "tool_call_update"):
            # These updates have no ``content`` list of ``type: tool_call``
            # blocks. The old handler returned before queueing anything, so
            # the transcript never showed the tool.
            queue.put((_tool_stream_kind(update), _tool_activity_payload(update)))
        # user_message_chunk is already the user turn. plan / usage / mode
        # updates are not the assistant answer, so they are not queued.

    def _handle_legacy_content(self, update: dict[str, Any], queue: Any) -> None:
        """Private ``content`` blocks: text / tool_call / tool_result.

        Used for prompt-result ``contentBlocks`` and pre-spec notifications.
        A dict without ``content`` is ignored so a bare key set is a no-op.
        """
        if "content" not in update:
            return
        content = update["content"]
        if isinstance(content, list):
            items = content
        elif isinstance(content, dict):
            items = [content]
        else:
            log.debug("ACP update content is neither list nor dict: %s", type(content))
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "")
                # What was wrong: a non-str text (object, number) was queued
                # as CHUNK. The session/update path already requires str.
                # Why: match _queue_text_blocks and skip a bad block.
                if isinstance(text, str):
                    queue.put((StreamQueueKind.CHUNK, text))
            elif item_type == "tool_call":
                queue.put((StreamQueueKind.TOOL_CALL, item))
            elif item_type == "tool_result":
                queue.put((StreamQueueKind.TOOL_RESULT, item))

    def _dispatch_notification(self, method: str, params: Any, msg_id: Any, queue: Any) -> None:
        if not isinstance(params, dict):
            params = {}
        # Permission is answered even after Stop. Dropping it would leave the
        # agent blocked in session/request_permission; the queue helper sends
        # cancelled and does not open the dialog.
        if method == "session/request_permission":
            self._queue_permission_request(params, msg_id, queue)
            return
        handled = False
        if not self._stop_requested:
            if method in ("notifications/session", "session/update"):
                self._handle_acp_update(params.get("update", {}), queue)
                handled = True
            elif method in ("notifications/agent", "agent/update"):
                self._handle_acp_update(params.get("update", params), queue)
                handled = True
        # A request we do not implement (fs/read_text_file, terminal/*, …)
        # still has an id. Leaving it unanswered blocks the agent. Notifications
        # have no id and are not replies.
        if msg_id is not None and not handled:
            self._reject_unknown_request(msg_id, method)

    def _reject_unknown_request(self, msg_id: Any, method: str) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.send_response(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception:
            log.exception("Failed to reject ACP method %s", method)

    def _queue_permission_request(self, params: dict[str, Any], msg_id: Any, queue: Any) -> None:
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            tool_call = {}
        options = params.get("options")
        stored = [opt for opt in options if isinstance(opt, dict)] if isinstance(options, list) else []
        # Keep the options so Approve/Reject can echo optionId. The HITL
        # dialog only returns a bool; the id has to live with the request.
        # The stop flag is read under the same lock stop() clears, so a
        # request that arrives as Stop runs is cancelled instead of stored
        # after the pending map was already swept.
        with self._permission_lock:
            if self._stop_requested:
                reply_cancelled = msg_id is not None
            else:
                reply_cancelled = False
                if msg_id is not None:
                    self._pending_permissions[msg_id] = stored
                else:
                    log.warning("ACP permission request missing id; cannot answer it")
        if reply_cancelled:
            if self._conn is not None:
                try:
                    self._conn.send_response(msg_id, result=_cancelled_permission())
                except Exception:
                    log.exception("Failed to cancel ACP permission %s", msg_id)
            return
        description = _permission_description(params)
        tool_name = _permission_tool_name(tool_call)
        queue.put((StreamQueueKind.APPROVAL_REQUIRED, description, tool_name, tool_call, msg_id))

    def _cancel_pending_permissions(self) -> None:
        """Answer every outstanding permission with the ACP ``cancelled`` outcome.

        Stop must reply, or the agent blocks inside
        ``session/request_permission`` and keeps the turn alive. The spec
        requires ``cancelled`` when the client sends ``session/cancel``.
        Pop under the lock so a concurrent Approve cannot answer twice.
        """
        lock = getattr(self, "_permission_lock", None)
        pending_map = getattr(self, "_pending_permissions", None)
        if lock is None or not isinstance(pending_map, dict):
            return
        with lock:
            pending_ids = list(pending_map)
            pending_map.clear()
        conn = self._conn
        if conn is None:
            return
        for request_id in pending_ids:
            try:
                conn.send_response(request_id, result=_cancelled_permission())
            except Exception:
                log.exception("Failed to cancel ACP permission %s", request_id)

    def _finish_stopped(self, queue: Any) -> None:
        queue.put((StreamQueueKind.STOPPED, None))

    def send(self, queue: Any, user_message: str, document_context: str | None, document_url: str | None, system_prompt: str | None = None, mcp_url: str | None = None, selection_text: str | None = None, stop_checker: Any = None, **kwargs: Any) -> None:
        """Send a message via ACP stdio. The subprocess is shut down before return."""
        # ``register_on_cancel(adapter.stop)`` can run before this worker
        # enters ``send()``. Clearing ``_stop_requested`` and ``_prompt_done``
        # on entry, and never reading ``stop_checker``, wiped that latch. The
        # UI showed Stopped while the CLI, session, and prompt still started.
        # Under the same lock ``stop()`` sets, honor an already-latched stop
        # or a true ``stop_checker`` and return without starting the process.
        # The latch is cleared only when this turn is not already stopped.
        checker_hit = callable(stop_checker) and bool(stop_checker())
        with self._permission_lock:
            if checker_hit or self._stop_requested:
                self._stop_requested = True
                already_stopped = True
            else:
                self._stop_requested = False
                already_stopped = False
        if already_stopped:
            self._finish_stopped(queue)
            self.shutdown()
            self._prompt_done.set()
            return

        self._prompt_done.clear()

        queue.put((StreamQueueKind.STATUS, f"Starting {self.get_display_name()}..."))

        try:
            try:
                self._ensure_connection(stop_checker)
            except Exception as e:
                if self._stop_requested:
                    self._finish_stopped(queue)
                else:
                    queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"Cannot start {self.get_display_name()} ACP. Is {self.get_binary_name()} installed? Error: {e}"))))
                return

            if self._stop_requested:
                self._finish_stopped(queue)
                return

            def on_notification(method: str, params: Any, msg_id: Any = None) -> None:
                self._dispatch_notification(method, params, msg_id, queue)

            # Agents emit session/update and session/request_permission while
            # session/new is in flight. Installing this callback afterwards
            # leaves _notify_callback None, and the reader drops them.
            # Register as soon as the process is up, before session/new.
            if self._conn:
                self._conn.set_notification_callback(on_notification)

            try:
                self._ensure_session(mcp_url=mcp_url)
            except Exception as e:
                if self._stop_requested:
                    self._finish_stopped(queue)
                else:
                    queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"Session creation failed: {e}"))))
                return

            if self._stop_requested:
                self._finish_stopped(queue)
                return

            queue.put((StreamQueueKind.STATUS, f"Sending to {self.get_display_name()}..."))

            prompt_blocks = self._build_prompt_blocks(user_message=user_message, document_context=document_context, system_prompt=system_prompt, selection_text=selection_text, document_url=document_url)

            try:
                if self._stop_requested or not self._conn:
                    self._finish_stopped(queue)
                    return

                result = self._conn.send_request("session/prompt", {"sessionId": self._session_id, "prompt": prompt_blocks}, timeout=600)

                if self._stop_requested:
                    self._finish_stopped(queue)
                    return

                # Process the final response
                if result:
                    stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                    log.info(f"Prompt completed: stop_reason={stop_reason}")
                    # Some ACP agents (Vibe) put final text in the prompt result.
                    # Same block types as the legacy content path; drain if present.
                    content_blocks = result.get("contentBlocks") or []
                    if content_blocks:
                        self._handle_acp_update({"content": content_blocks}, queue)

                queue.put((StreamQueueKind.STREAM_DONE, None))

            except TimeoutError:
                if self._stop_requested:
                    self._finish_stopped(queue)
                else:
                    queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.get_display_name()} prompt timed out"))))
            except Exception as e:
                if self._stop_requested:
                    self._finish_stopped(queue)
                else:
                    log.exception("Prompt execution failed")
                    queue.put((StreamQueueKind.ERROR, format_error_payload(e)))
        finally:
            # Every send must stop the connection on success, error, and
            # cancel. Leaving the child and its reader alive leaks another
            # agent that can still edit the document on the next message.
            # stop() may already have killed it; ACPConnection.stop() is safe
            # to call again.
            self.shutdown()
            self._prompt_done.set()

    def stop(self) -> None:
        """Cancel the prompt turn and terminate the ACP subprocess.

        ACP does not define ``session/interrupt``. Cancellation is the
        ``session/cancel`` notification, every outstanding permission must
        be ``cancelled``, and the subprocess has to be terminated. Otherwise
        the UI shows Stopped while the agent keeps editing.
        """
        # Same lock send() holds while it decides whether to clear the latch,
        # so a Stop that lands as send() begins cannot be wiped by that reset.
        with self._permission_lock:
            self._stop_requested = True
        conn = self._conn
        if conn is not None and conn.is_alive and self._session_id:
            try:
                conn.send_notification("session/cancel", {"sessionId": self._session_id})
            except Exception:
                log.exception("ACP session/cancel failed")
        # Reply while stdin is still open. shutdown() closes it.
        self._cancel_pending_permissions()
        self.shutdown()
        self._prompt_done.set()

    def submit_approval(self, request_id: Any, approved: bool) -> None:
        """Submit a schema-valid permission result for one HITL dialog choice.

        Agents reject ``{"approved": bool}``. ACP requires
        ``outcome.selected`` plus an ``optionId`` from the request, or
        ``outcome.cancelled``. The options were stored when the request
        arrived so this can echo one of those ids.
        """
        if not self._conn or not self._conn.is_alive:
            log.warning("Cannot submit approval, ACP connection is dead")
            return

        with self._permission_lock:
            options = self._pending_permissions.pop(request_id, None)
        if options is None:
            # Stop already answered with cancelled, or the id is unknown.
            log.warning("No pending ACP permission for id=%s", request_id)
            return

        try:
            self._conn.send_response(request_id, result=_permission_result(options, approved))
        except Exception:
            log.exception("Failed to submit approval")

    def shutdown(self) -> None:
        """Stop the ACP subprocess and drop the session.

        Called at the end of every ``send`` and from ``stop``. Pending
        permissions are cancelled first so a turn that errors while a
        dialog is open still answers the agent before stdin closes.
        """
        conn = self._conn
        if conn is not None:
            try:
                conn.set_notification_callback(None)
            except Exception:
                log.exception("Failed to clear ACP notification callback")
        self._cancel_pending_permissions()
        if conn is not None:
            try:
                conn.stop()
            except Exception:
                log.exception("ACP shutdown failed")
            if self._conn is conn:
                self._conn = None
        self._session_id = None
