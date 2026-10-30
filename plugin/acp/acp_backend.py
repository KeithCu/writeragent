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

import glob
import logging
import os
import shlex
import shutil
import threading
from typing import Any, Optional, List, Tuple, Mapping

from plugin.acp.base import AgentBackend
from plugin.acp.acp_connection import ACPConnection, ACPTimeoutError
from plugin.framework.client.provider_detection import get_provider_from_endpoint
from plugin.framework.config import get_api_key_for_endpoint, get_current_endpoint
from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.errors import format_error_payload
from plugin.framework.worker_pool import run_in_background
from plugin.version import EXTENSION_VERSION

log = logging.getLogger(__name__)

# ACP protocol version (integer per SDK)
_ACP_PROTOCOL_VERSION = 1

# PermissionOption.kind values from the ACP schema. Prefer the "_once"
# choice so Approve does not permanently allow later tool calls.
_ALLOW_KINDS = ("allow_once", "allow_always")
_REJECT_KINDS = ("reject_once", "reject_always")
_SESSION_UPDATES = ("notifications/session", "session/update")
_AGENT_UPDATES = ("notifications/agent", "agent/update")
# shutil.which on Windows returns the PATHEXT hit (hermes.exe, hermes.cmd,
# hermes.bat). Those stems are the same CLI as the POSIX basename.
_WINDOWS_CLI_SUFFIXES = frozenset({".exe", ".cmd", ".bat"})


def _split_cli_args(args_str: str, *, posix: bool) -> list[str]:
    """Split settings args. Windows keeps backslashes, then drops wrapping quotes.

    ``posix=True`` treats ``\\`` as an escape, so ``C:\\new\\tmp`` becomes
    ``C:newtmp``. ``posix=False`` keeps the backslashes and scans Windows-style
    tokens without shlex.
    """
    if posix:
        return shlex.split(args_str, posix=True)

    tokens: list[str] = []
    token_chars: list[str] = []
    has_token = False
    in_quotes = False
    i = 0
    n = len(args_str)

    def flush() -> None:
        nonlocal has_token
        if not has_token:
            return
        tok = "".join(token_chars)
        if tok == "''":
            tok = ""
        tokens.append(tok)
        token_chars.clear()
        has_token = False

    while i < n:
        c = args_str[i]
        if c == "\\" and i + 1 < n and args_str[i + 1] == '"':
            token_chars.append('"')
            has_token = True
            i += 2
        elif c == '"':
            in_quotes = not in_quotes
            has_token = True
            i += 1
        elif c.isspace() and not in_quotes:
            flush()
            i += 1
        else:
            token_chars.append(c)
            has_token = True
            i += 1

    if in_quotes:
        raise ValueError("No closing quotation")

    flush()

    return tokens


def _directory_from_document_url(document_url: str | None) -> str | None:
    """Parent directory of a local ``file://`` document, or None when it is not a local file.

    A Windows UNC host becomes ``\\\\host\\...``; any other non-localhost host returns None.
    """
    from urllib.parse import unquote, urlparse

    text = str(document_url or "").strip()
    if text.startswith("file:/") and not text.startswith("file://"):
        text = "file://" + text[len("file:") :]
    if not text.startswith("file://"):
        return None
    parsed = urlparse(text)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    # A non-localhost host is UNC only on Windows; localhost stays on the drive-letter branch.
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        if os.name != "nt":
            return None
        path = "\\\\" + parsed.netloc + path.replace("/", "\\")
    elif os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:].replace("/", "\\")
    if not path:
        return None
    if os.path.isdir(path):
        return path
    parent = os.path.dirname(path)
    if parent and os.path.isdir(parent):
        return parent
    return None


def _session_cwd(document_url: str | None) -> str:
    """Directory the agent should treat as the project root.

    ``os.getcwd()`` is LibreOffice's launch directory. A saved document's
    folder is the root. An unsaved document has no folder, so use home.
    """
    found = _directory_from_document_url(document_url)
    if found:
        return found
    return os.path.expanduser("~")


_LIMIT_STOP_STATUS = {
    "max_tokens": "Stopped: maximum tokens",
    "max_turn_requests": "Stopped: turn request limit",
}
# session/prompt dies after this long with no accepted stdout line.
_PROMPT_IDLE_SEC = 600
_INITIALIZE_TIMEOUT_SEC = 15
_SESSION_NEW_TIMEOUT_SEC = 30
# Stop sends session/cancel and keeps the CLI. If the prompt has not
# returned by then, the agent ignored cancel and the tree is killed.
_CANCEL_GRACE_SEC = 3.0


class _TurnStopped(Exception):
    """This turn was stopped before its JSON-RPC request was sent."""


def _cli_basename_matches(path: str, binary_name: str) -> bool:
    """True when ``path`` is this CLI, including Windows launcher suffixes.

    ``shutil.which`` on Windows returns ``hermes.exe``, ``hermes.cmd``, or
    ``hermes.bat``. Comparing that basename to ``binary_name``
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


def _binary_candidates(binary_name: str) -> list[str]:
    """Executable paths outside a short desktop PATH, in search order."""
    home = os.path.expanduser("~")
    names: tuple[str, ...] = (binary_name,)
    if os.name == "nt":
        names = (binary_name, f"{binary_name}.cmd", f"{binary_name}.exe", f"{binary_name}.bat")
    directories = [
        "/usr/local/bin",
        "/opt/homebrew/bin",
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".opencode", "bin"),
    ]
    # Any nvm install is enough. Sorted so the choice does not depend on glob order.
    nvm_bins = os.path.join(home, ".nvm", "versions", "node", "*", "bin")
    directories.extend(sorted(glob.glob(nvm_bins)))
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            directories.append(os.path.join(appdata, "npm"))
    return [os.path.join(directory, name) for directory in directories for name in names]


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


def _content_blocks(content: Any) -> list[Any] | None:
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return content
    return None


def _queue_text_blocks(content: Any, queue: Any, kind: StreamQueueKind) -> None:
    blocks = _content_blocks(content)
    if blocks is None:
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
    """``TOOL_RESULT`` only for a finished call.

    Pending and in_progress stay activity; terminal status or content with no status is the result.
    """
    if update.get("sessionUpdate") != "tool_call_update":
        return StreamQueueKind.TOOL_CALL
    status = update.get("status")
    if status in ("pending", "in_progress"):
        return StreamQueueKind.TOOL_CALL
    if status is not None:
        return StreamQueueKind.TOOL_RESULT
    if "content" in update or "rawOutput" in update:
        return StreamQueueKind.TOOL_RESULT
    return StreamQueueKind.TOOL_CALL


def _agent_backend_settings() -> tuple[str, str]:
    from plugin.framework.config import get_config

    path = str(get_config("agent_backend.path") or "").strip()
    args_str = str(get_config("agent_backend.args") or "").strip()
    return path, args_str


class ACPBackend(AgentBackend):
    """Base class for ACP-based agent backends. Subclasses in `plugin.acp.registry` set these attributes."""

    binary_name: str = ""
    binary_prefixes: tuple[str, ...] = ()
    api_key_env_by_provider: Mapping[str, str] = {}
    default_extra_args: Tuple[str, ...] = ()
    _stop_requested: bool
    _state_lock: threading.Lock
    _send_lock: threading.Lock
    _pending_permissions: dict[Any, tuple[ACPConnection, list[Any]]]
    _turn: int
    _turn_pending: bool
    _stop_turn: int
    _ready: bool
    _session_url: str | None
    _session_mcp_url: str | None
    _session_prompted: bool
    _prompt_inflight: bool
    _prompt_done: threading.Event
    _inflight_token: Any | None
    _closed: bool

    def __init__(self) -> None:
        self._conn: ACPConnection | None = None
        self._session_id: str | None = None
        self._session_url = None
        self._session_mcp_url = None
        self._session_prompted = False
        self._ready = False
        self._stop_requested = False
        self._closed = False
        self._inflight_token = None
        # prepare_turn publishes _turn before Stop is registered. send()
        # consumes that publication. A send that was not prepared opens a
        # new turn, so a Stop from the previous message cannot stick.
        self._turn = 0
        self._turn_pending = False
        self._stop_turn = 0
        self._prompt_inflight = False
        self._prompt_done = threading.Event()
        self._prompt_done.set()
        self._binary_path: Optional[str] = None
        self._extra_args: List[str] = []
        # request id -> options from session/request_permission. The same
        # lock guards the stop latch so send() cannot clear a Stop that
        # landed as this turn began.
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending_permissions = {}
        self._binary_cache_key: tuple[str, str, str] | None = None
        self._binary_cache_val: str | None = None
        self._load_config()

    def _resolve_binary(self, path_setting: str, args_str: str) -> str | None:
        """Resolve binary path with in-instance cache for PATH hits."""
        if path_setting and os.path.isfile(path_setting):
            return path_setting
        if path_setting:
            log.warning("ACP agent_backend.path is not a file (%s); searching PATH", path_setting)

        key = (self.binary_name, path_setting, args_str)
        cached_key = getattr(self, "_binary_cache_key", None)
        cached_val = getattr(self, "_binary_cache_val", None)
        if cached_key == key and cached_val is not None:
            if os.path.isfile(cached_val) and os.access(cached_val, os.X_OK):
                return cached_val

        found = self._find_binary()
        if found is not None:
            self._binary_cache_key = key
            self._binary_cache_val = found
        return found

    def _launch_extra_args(self, binary: str | None, args_str: str) -> list[str]:
        try:
            extra = _split_cli_args(args_str, posix=(os.name != "nt")) if args_str else []
        except ValueError as e:
            raise ValueError(f"unbalanced quote: {e}") from e
        if not extra and binary and self._binary_matches(binary):
            defaults = list(self.default_extra_args)
            if defaults:
                return defaults
        return extra

    def _load_config(self) -> None:
        """Load configuration from WriterAgent settings."""
        path, args_str = _agent_backend_settings()
        self._binary_path = self._resolve_binary(path, args_str)

        # str.split() breaks a quoted path into several argv entries.
        # posix=False on Windows so backslashes in paths stay literal.
        try:
            self._extra_args = self._launch_extra_args(self._binary_path, args_str)
        except ValueError:
            self._extra_args = []
            raise

    def _binary_matches(self, path: str) -> bool:
        """True when ``path`` is this backend's CLI, including Windows launcher suffixes."""
        if _cli_basename_matches(path, self.binary_name):
            return True
        name = os.path.basename(path).lower()
        return any(name.startswith(prefix) for prefix in self.binary_prefixes)

    def _find_binary(self) -> str | None:
        """Find the binary in PATH or install dirs a short desktop PATH misses.

        A GUI-launched LibreOffice often has ``/usr/bin:/bin`` only. npm,
        nvm, Homebrew, and OpenCode installs live outside that. One list
        serves every backend: the file name is the discriminant.
        """
        binary_name = self.binary_name
        found = shutil.which(binary_name)
        if found:
            return found
        for candidate in _binary_candidates(binary_name):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def get_env_vars(self) -> dict[str, str]:
        """Return environment variables to pass to subprocess."""
        if not self.api_key_env_by_provider:
            return {}
        endpoint = get_current_endpoint()
        provider = get_provider_from_endpoint(endpoint)
        if not provider:
            return {}
        env_name = self.api_key_env_by_provider.get(provider)
        if not env_name:
            return {}
        key = get_api_key_for_endpoint(endpoint)
        if not key:
            return {}
        log.info("Using %s from general settings", env_name)
        return {env_name: key}

    def is_available(self, ctx: Any) -> bool:
        """Check if binary is installed."""
        path, args_str = _agent_backend_settings()
        binary = self._resolve_binary(path, args_str)
        extra = self._launch_extra_args(binary, args_str)

        if self._send_lock.acquire(blocking=False):
            try:
                self._binary_path = binary
                self._extra_args = extra
            finally:
                self._send_lock.release()

        if binary:
            log.info("%s binary found: %s", self.display_name, binary)
            return True
        log.info("%s binary not found", self.display_name)
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

    def _slot_lost_locked(self, conn: ACPConnection | None = None) -> bool:
        """Caller holds ``_state_lock``. Latch Stop when this conn is no longer the slot."""
        if self._closed or (conn is not None and self._conn is not conn):
            self._stop_requested = True
            self._stop_turn = self._turn
            return True
        return False

    def _ensure_connection(self, stop_checker: Any = None) -> None:
        """Start the ACP subprocess, or keep the one this sidebar already has.

        A live initialized child is the slot. A live child that never
        finished ``initialize`` (Stop during the startup wait) is reused
        for the handshake. A dead child is dropped here, not left on the slot.
        """
        with self._state_lock:
            if self._slot_lost_locked():
                return
        existing = self._conn
        if existing is not None and existing.is_healthy and self._ready:
            return
        if existing is not None and existing.is_healthy:
            conn = existing
        else:
            if existing is not None:
                self._drop_connection()
            if self._stop_requested:
                return
            if not self._binary_path:
                raise RuntimeError(f"{self.display_name} binary not found. Install {self.binary_name} and ensure it's in PATH.")

            cmd_line = [self._binary_path]
            cmd_line.extend(self._extra_args)

            # ACPConnection.start merges os.environ itself. Passing a full copy
            # here merged the process environment twice.
            conn = ACPConnection(cmd_line=cmd_line, env=self.get_env_vars() or None)
            with self._state_lock:
                self._conn = conn
                self._ready = False
            conn.start()

            with self._state_lock:
                lost_slot = self._slot_lost_locked(conn)
            if lost_slot:
                conn.stop()
                return

            # Honor Stop before sending initialize; fail fast if child died during spawn.
            if self._startup_stopped(stop_checker):
                return
            if not conn.is_alive:
                detail = conn.stderr_text().strip()
                message = f"{self.display_name} ACP process failed to start."
                if detail:
                    message = f"{message} {detail[:500]}"
                raise RuntimeError(message)

        # Initialize handshake. ACP fs capability names are camelCase;
        # snake_case keys are ignored and do not turn the features off.
        try:
            result = self._request(
                conn,
                "initialize",
                {
                    "protocolVersion": _ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "WriterAgent", "version": EXTENSION_VERSION},
                },
                _INITIALIZE_TIMEOUT_SEC,
            )
            if isinstance(result, dict) and "protocolVersion" in result and result["protocolVersion"] != _ACP_PROTOCOL_VERSION:
                log.warning(
                    "ACP protocolVersion mismatch: server reported %s, expected %s",
                    result["protocolVersion"],
                    _ACP_PROTOCOL_VERSION,
                )
            with self._state_lock:
                lost_slot = self._slot_lost_locked(conn)
                if not lost_slot:
                    self._ready = True
            if lost_slot:
                conn.stop()
                return
            log.info(f"ACP initialized: {result}")
        except _TurnStopped:
            # Stop won before initialize was sent. Leave the child so the
            # next message can finish the handshake. _ready stays false.
            return
        except Exception as exc:
            with self._state_lock:
                if self._slot_lost_locked(conn):
                    return
            log.exception("ACP initialize failed")
            # A crash during the handshake used to say only "process
            # terminated". The stderr tail is the CLI's own reason.
            detail = conn.stderr_text().strip() if not conn.is_alive else ""
            self._release_connection(conn)
            if detail:
                raise RuntimeError(f"{self.display_name} ACP process failed to start. {detail[:500]}") from exc
            raise

    def _ensure_session(self, mcp_url: str | None = None, document_url: str | None = None) -> None:
        """Open an ACP session for this document, or keep the one it already has.

        The same document URL reuses ``sessionId``. A different URL, including
        the first save, starts another session on this process.
        """
        from plugin.framework.config import get_config
        from plugin.framework.config_schema import as_bool

        url_key = document_url or ""
        effective_mcp = mcp_url if mcp_url and as_bool(get_config("mcp.mcp_enabled")) else None

        with self._state_lock:
            if self._slot_lost_locked():
                return
            # The session keeps the mcpServers from session/new, so a new MCP URL is a new session.
            if self._session_id and self._session_url == url_key and self._session_mcp_url == effective_mcp:
                return
            # Read the connection slot once; raise if there is no process.
            conn = self._conn
            if conn is None:
                raise RuntimeError(f"{self.display_name} ACP process is not running.")

        # mcp_servers is required by the ACP schema. A disabled MCP server
        # must not be handed to session/new (effective_mcp is None then).
        mcp_servers = []
        if effective_mcp is not None:
            mcp_servers.append({"url": effective_mcp, "name": "writeragent", "type": "http", "headers": []})

        params = {"cwd": _session_cwd(document_url), "mcpServers": mcp_servers}

        try:
            result = self._request(conn, "session/new", params, _SESSION_NEW_TIMEOUT_SEC)
            session_id = result.get("sessionId") if isinstance(result, dict) else None
            # An empty id used to be stored and sent on session/prompt.
            if not isinstance(session_id, str) or not session_id.strip():
                raise RuntimeError(f"{self.display_name} ACP session/new returned no sessionId.")
            with self._state_lock:
                if self._slot_lost_locked(conn):
                    return
                self._session_id = session_id
                self._session_url = url_key
                self._session_mcp_url = effective_mcp
                self._session_prompted = False
            log.debug(f"ACP session created: {self._session_id}")
        except _TurnStopped:
            # Stop won before session/new was sent. Not a session failure,
            # and the process is still the one the next message should use.
            raise
        except Exception:
            with self._state_lock:
                if self._slot_lost_locked(conn):
                    return
            log.exception("ACP session creation failed")
            # The process can retry session/new. Drop it only when it has died.
            if self._conn is conn and not conn.is_healthy:
                self._drop_connection()
            raise

    def _build_prompt_blocks(self, user_message: str, document_context: Optional[str] = None, system_prompt: Optional[str] = None, document_url: Optional[str] = None, *, include_system: bool = True) -> tuple[list[dict[str, Any]], bool]:
        """Build ACP prompt content blocks.

        The first prompt of a session carries the manual. Later prompts do
        not: the CLI already has it. The document snapshot is sent every
        time, because the file changes between messages.
        """
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")
        included_manual = False

        if is_slash_command:
            # For slash commands, only send the command itself
            prompt_blocks.append({"type": "text", "text": user_message})
        else:
            if include_system and system_prompt:
                prompt_blocks.append({"type": "text", "text": system_prompt})
                included_manual = True
            # Add document context if provided
            if document_context:
                prompt_blocks.append({"type": "text", "text": f"[DOCUMENT CONTENT]\n{document_context}"})
            # Add document URL if provided
            if document_url:
                prompt_blocks.append({"type": "text", "text": f"Document URL: {document_url}"})
            # Always add the user message last
            prompt_blocks.append({"type": "text", "text": user_message})

        return prompt_blocks, included_manual

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
            return
        self._handle_legacy_content(update, queue)

    def _handle_legacy_content(self, update: dict[str, Any], queue: Any) -> None:
        """Private ``content`` blocks: text / tool_call / tool_result.

        Used for prompt-result ``contentBlocks`` and pre-spec notifications.
        A dict without ``content`` is ignored so a bare key set is a no-op.
        """
        if "content" not in update:
            return
        content = update["content"]
        items = _content_blocks(content)
        if items is None:
            log.debug("ACP update content is neither list nor dict: %s", type(content))
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                _queue_text_blocks(item, queue, StreamQueueKind.CHUNK)
            elif item_type == "tool_call":
                queue.put((StreamQueueKind.TOOL_CALL, item))
            elif item_type == "tool_result":
                queue.put((StreamQueueKind.TOOL_RESULT, item))

    def _dispatch_notification(self, conn: ACPConnection, method: str, params: Any, msg_id: Any, queue: Any) -> None:
        if not isinstance(params, dict):
            params = {}
        # Permission is answered even after Stop. Dropping it would leave the
        # agent blocked in session/request_permission; the queue helper sends
        # cancelled and does not open the dialog.
        if method == "session/request_permission":
            self._queue_permission_request(conn, params, msg_id, queue)
            return
        if method in _SESSION_UPDATES or method in _AGENT_UPDATES:
            if not self._stop_requested:
                default: Any = {} if method in _SESSION_UPDATES else params
                self._handle_acp_update(params.get("update", default), queue)
            if msg_id is not None:
                conn.send_response(msg_id, result={})
            return
        if msg_id is not None:
            conn.send_response(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def _queue_permission_request(self, conn: ACPConnection, params: dict[str, Any], msg_id: Any, queue: Any) -> None:
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            tool_call = {}
        options = params.get("options")
        stored = [opt for opt in options if isinstance(opt, dict)] if isinstance(options, list) else []
        # Permission request requires a valid id to answer.
        if msg_id is None:
            log.warning("ACP permission request missing id; cannot answer it")
            return
        # Unhashable request ids cannot be stored in _pending_permissions; cancel immediately.
        try:
            hash(msg_id)
        except TypeError:
            log.warning("ACP permission request id is unhashable; cancelling without storing it")
            conn.send_response(msg_id, result=_cancelled_permission())
            return
        # Keep the options so Approve/Reject can echo optionId. The HITL
        # dialog only returns a bool; the id has to live with the request.
        # The stop flag is read under the same lock stop() clears, so a
        # request that arrives as Stop runs is cancelled instead of stored
        # after the pending map was already swept.
        with self._state_lock:
            reply_cancelled = self._stop_requested
            if not reply_cancelled:
                self._pending_permissions[msg_id] = (conn, stored)
        if reply_cancelled:
            conn.send_response(msg_id, result=_cancelled_permission())
            return
        description = _permission_description(params)
        tool_name = _permission_tool_name(tool_call)
        queue.put((StreamQueueKind.APPROVAL_REQUIRED, description, tool_name, tool_call, msg_id))

    def _pop_pending_permission_ids(self) -> list[tuple[Any, ACPConnection]]:
        """Drop the pending map. Caller holds ``_state_lock``."""
        pending = [(req_id, conn) for req_id, (conn, _options) in self._pending_permissions.items()]
        self._pending_permissions.clear()
        return pending

    def _clear_slot_locked(self, conn: ACPConnection | None) -> None:
        """Forget ``conn`` and its session. Caller holds ``_state_lock``.

        A different connection already in the slot is left alone, including
        its session. ``conn`` None clears whatever the slot holds.
        """
        if conn is not None and self._conn is not conn:
            return
        self._conn = None
        self._ready = False
        self._session_id = None
        self._session_url = None
        self._session_mcp_url = None
        self._session_prompted = False

    def _cancel_pending_permissions(self) -> None:
        """Answer every outstanding permission with the ACP ``cancelled`` outcome.

        Stop must reply, or the agent blocks inside
        ``session/request_permission`` and keeps the turn alive. The spec
        requires ``cancelled`` when the client sends ``session/cancel``.
        Pop under the lock so a concurrent Approve cannot answer twice.
        """
        with self._state_lock:
            pending_permissions = self._pop_pending_permission_ids()
        for request_id, conn in pending_permissions:
            conn.send_response(request_id, result=_cancelled_permission())

    def _finish_stopped(self, queue: Any) -> None:
        queue.put((StreamQueueKind.STOPPED, None))

    def _report_failure(self, queue: Any, payload: Any) -> None:
        """Stopped when this turn was cancelled. Otherwise queue ``payload``."""
        if self._stop_requested:
            self._finish_stopped(queue)
            return
        queue.put((StreamQueueKind.ERROR, payload))

    def prepare_turn(self) -> None:
        """Publish a turn id before Stop can be registered.

        ``send_handlers`` calls this, then ``register_on_cancel(stop)``.
        A Stop that lands before ``send()`` records this id. ``send()``
        consumes the publication. A Stop from the previous turn recorded
        an older id and does not abort this one.
        """
        with self._state_lock:
            self._turn += 1
            self._turn_pending = True
            self._stop_turn = 0
            self._stop_requested = False

    def _turn_already_stopped(self, stop_checker: Any) -> bool:
        """True when this send must return Stopped without starting work.

        prepare_turn publishes an id that this send consumes. A send that was
        not published opens a new turn, so a previous Stop cannot stick.
        """
        checker_hit = callable(stop_checker) and bool(stop_checker())
        with self._state_lock:
            if self._turn_pending:
                self._turn_pending = False
            else:
                self._turn += 1
                self._stop_turn = 0
                self._stop_requested = False
            if checker_hit or self._stop_turn == self._turn:
                self._stop_turn = self._turn
                self._stop_requested = True
                return True
            self._stop_requested = False
            return False

    def _request(self, conn: ACPConnection, method: str, params: Any, timeout: float) -> Any:
        """send_request marked in flight so Stop can bound it."""
        token = object()
        # The stop latch and inflight flag share _state_lock so Stop cannot slip between the check and the enqueue.
        with self._state_lock:
            if self._stop_requested and self._stop_turn == self._turn:
                raise _TurnStopped()
            self._prompt_done.clear()
            self._prompt_inflight = True
            self._inflight_token = token
            try:
                handle = conn.begin_request(method, params)
            except Exception:
                self._prompt_inflight = False
                if self._inflight_token is token:
                    self._inflight_token = None
                self._prompt_done.set()
                raise
        try:
            return conn.await_response(handle, timeout)
        finally:
            with self._state_lock:
                self._prompt_inflight = False
                if self._inflight_token is token:
                    self._inflight_token = None
            self._prompt_done.set()

    def _arm_cancel_grace(self, token: Any, conn: ACPConnection | None) -> None:
        """Kill the CLI if this turn's request is still waiting after the grace."""

        def _watch() -> None:
            # Activity after Stop does not extend this. The idle prompt
            # clock would, and a wedged agent would keep editing.
            if self._prompt_done.wait(_CANCEL_GRACE_SEC):
                return
            self._kill_turn_if_still_inflight(token, conn)

        run_in_background(_watch, name="acp-cancel-grace", dedicated=True)

    def _kill_turn_if_still_inflight(self, token: Any, conn: ACPConnection | None) -> None:
        """Stop the CLI captured for ``token`` when that request is still in flight."""
        # Check token and clear slot under _state_lock so a subsequent send does not adopt a killed connection.
        with self._state_lock:
            if token is None or self._inflight_token is not token or not self._prompt_inflight or self._conn is not conn:
                return
            pending_permissions = self._pop_pending_permission_ids()
            self._clear_slot_locked(conn)
        log.info("ACP cancel was not answered within %ss; killing the CLI", _CANCEL_GRACE_SEC)
        for request_id, req_conn in pending_permissions:
            req_conn.send_response(request_id, result=_cancelled_permission())
        if conn is None:
            return
        conn.set_notification_callback(None)
        try:
            conn.stop()
        except Exception:
            log.exception("ACP shutdown failed")

    def send(self, queue: Any, user_message: str, document_context: str | None, document_url: str | None, system_prompt: str | None = None, mcp_url: str | None = None, stop_checker: Any = None) -> None:
        """Send one prompt. The CLI stays up for the next message on this sidebar."""
        # Serialize send() calls on the retained CLI. stop() and shutdown() do not take this lock, so session/cancel still runs.
        with self._send_lock:
            with self._state_lock:
                if self._closed:
                    self._finish_stopped(queue)
                    return
            if self._turn_already_stopped(stop_checker):
                self._finish_stopped(queue)
                return

            queue.put((StreamQueueKind.STATUS, f"Starting {self.display_name}..."))

            try:
                try:
                    self._ensure_connection(stop_checker)
                except Exception as e:
                    self._report_failure(queue, format_error_payload(RuntimeError(f"Cannot start {self.display_name} ACP. Is {self.binary_name} installed? Error: {e}")))
                    return

                if self._stop_requested:
                    self._finish_stopped(queue)
                    return

                # Agents emit session/update and session/request_permission while
                # session/new is in flight. Installing this callback afterwards
                # leaves _notify_callback None, and the reader drops them.
                # Register as soon as the process is up, before session/new.
                conn = self._conn
                if conn is not None:
                    # Bind connection as default arg so callbacks use this exact connection.
                    def on_notification(method: str, params: Any, msg_id: Any = None, bound_conn: ACPConnection = conn) -> None:
                        self._dispatch_notification(bound_conn, method, params, msg_id, queue)

                    conn.set_notification_callback(on_notification)

                try:
                    self._ensure_session(mcp_url=mcp_url, document_url=document_url)
                except Exception as e:
                    self._report_failure(queue, format_error_payload(RuntimeError(f"Session creation failed: {e}")))
                    return

                if self._stop_requested:
                    self._finish_stopped(queue)
                    return

                queue.put((StreamQueueKind.STATUS, f"Sending to {self.display_name}..."))

                prompt_blocks, included_manual = self._build_prompt_blocks(user_message=user_message, document_context=document_context, system_prompt=system_prompt, document_url=document_url, include_system=not self._session_prompted)

                try:
                    # Single local read of self._conn; check stop and none.
                    conn = self._conn
                    if self._stop_requested:
                        self._finish_stopped(queue)
                        return
                    if conn is None:
                        self._report_failure(queue, format_error_payload(RuntimeError(f"{self.display_name} ACP process is not running.")))
                        return

                    result = self._request(conn, "session/prompt", {"sessionId": self._session_id, "prompt": prompt_blocks}, _PROMPT_IDLE_SEC)
                    # A slash-first turn, or a turn with no system prompt, used to set the flag anyway,
                    # so later turns skipped the manual. The flag now follows the block that was actually built.
                    if included_manual:
                        self._session_prompted = True

                    if self._stop_requested:
                        self._finish_stopped(queue)
                        return

                    self._finish_prompt_result(result, queue)

                except _TurnStopped:
                    self._finish_stopped(queue)
                except ACPTimeoutError:
                    # No accepted stdout for the idle window. The CLI is wedged.
                    self._report_failure(queue, format_error_payload(RuntimeError(f"{self.display_name} prompt timed out")))
                    if not self._stop_requested:
                        self._drop_connection()
                except Exception as e:
                    # When cancel grace killed this conn, the slot was cleared (self._conn is not conn).
                    # A new send may have already prepared or started. Calling shutdown() or reporting
                    # error here would break that next send or emit Error after Stop. If self._conn
                    # is not conn and the child is dead, finish stopped without dropping the new connection.
                    if self._stop_requested or (conn is not None and not conn.is_healthy and self._conn is not conn):
                        self._finish_stopped(queue)
                    else:
                        log.exception("Prompt execution failed")
                        self._report_failure(queue, format_error_payload(e))
                        # The exception can land before ``conn`` is known to be
                        # live. A dead child is dropped. A JSON-RPC error on a
                        # live child keeps the process for the next message.
                        if conn is not None and not conn.is_healthy:
                            self._drop_connection()
            finally:
                # The process stays. Drop the callback so a late notification
                # does not write into this turn's queue. The next send installs
                # its own. shutdown() may already have cleared the connection.
                conn = self._conn
                if conn is not None:
                    conn.set_notification_callback(None)
                self._cancel_pending_permissions()

    def _finish_prompt_result(self, result: Any, queue: Any) -> None:
        """Queue the terminal event for a ``session/prompt`` result."""
        # ACP refusal should be surfaced as an error; partial answers are kept.
        if not isinstance(result, dict):
            if result:
                queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.display_name} returned an unexpected prompt result."))))
            else:
                queue.put((StreamQueueKind.STREAM_DONE, None))
            return
        stop_reason = str(result.get("stopReason") or result.get("stop_reason") or "")
        log.info("Prompt completed: stop_reason=%s", stop_reason)
        # Some ACP agents (Vibe) put final text in the prompt result.
        # Same block types as the legacy content path; drain if present.
        content_blocks = result.get("contentBlocks") or []
        if content_blocks:
            self._handle_acp_update({"content": content_blocks}, queue)
        if stop_reason == "cancelled" or self._stop_requested:
            self._finish_stopped(queue)
            return
        if stop_reason == "refusal":
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.display_name} refused the prompt."))))
            return
        limit = _LIMIT_STOP_STATUS.get(stop_reason)
        if limit:
            queue.put((StreamQueueKind.STATUS, limit))
        queue.put((StreamQueueKind.STREAM_DONE, None))

    def stop(self) -> None:
        """Cancel the in-flight prompt and keep a healthy CLI.

        ACP does not define ``session/interrupt``. Cancellation is the
        ``session/cancel`` notification, and every outstanding permission
        must be ``cancelled``. Killing here threw away the session on every
        Stop. The process dies only if the request is still in flight after
        ``_CANCEL_GRACE_SEC``, or when the sidebar calls ``shutdown``.

        The cancel bytes are queued ahead of any later kill. This does not
        wait on the success path. Stop waits up to the grace for the prompt
        result. Activity after Stop does not extend that wait.
        """
        # Same lock send() holds while it decides whether to clear the latch,
        # so a Stop that lands as send() begins cannot be wiped by that reset.
        with self._state_lock:
            self._stop_turn = self._turn
            self._stop_requested = True
            inflight = self._prompt_inflight
            token = self._inflight_token
            # Read _conn and _session_id under _state_lock to avoid sending cancel to a swapped connection.
            conn = self._conn
            session_id = self._session_id
        if inflight and conn is not None and conn.is_alive and session_id:
            conn.send_notification("session/cancel", {"sessionId": session_id})
        # Reply while stdin is still open. A later shutdown closes it.
        self._cancel_pending_permissions()
        if inflight:
            self._arm_cancel_grace(token, conn)

    def submit_approval(self, request_id: Any, approved: bool) -> None:
        """Submit a schema-valid permission result for one HITL dialog choice."""
        # Pop under _state_lock first; reply on stored connection rather than re-reading self._conn.
        with self._state_lock:
            pending = self._pending_permissions.pop(request_id, None)
        if pending is None:
            # Stop already answered with cancelled, or the id is unknown.
            log.warning("No pending ACP permission for id=%s", request_id)
            return

        conn, options = pending
        if not conn.is_alive:
            log.warning("Cannot submit approval, ACP connection is dead")
            return

        conn.send_response(request_id, result=_permission_result(options, approved))

    def shutdown(self) -> None:
        """Kill the CLI and drop the session.

        The panel drops this object; a shut-down adapter is not reused.
        Internal CLI drops go through _drop_connection.
        """
        with self._state_lock:
            self._closed = True
            conn = self._conn
            self._clear_slot_locked(conn)
        self._cancel_pending_permissions()
        self._release_connection(conn)

    def _drop_connection(self) -> None:
        """Drop the current connection so a replacement can be spawned."""
        with self._state_lock:
            conn = self._conn
            self._clear_slot_locked(conn)
        self._cancel_pending_permissions()
        self._release_connection(conn)

    def _release_connection(self, conn: ACPConnection | None) -> None:
        """Reap ``conn`` and forget its session when it is still the slot."""
        # Stop captured process and drop the slot under _state_lock only if it is still this connection.
        if conn is not None:
            conn.set_notification_callback(None)
            try:
                conn.stop()
            except Exception:
                log.exception("ACP shutdown failed")
        with self._state_lock:
            self._clear_slot_locked(conn)
