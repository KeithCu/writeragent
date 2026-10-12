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
from typing import Any, Optional, Dict, List, Tuple

from plugin.acp.base import AgentBackend
from plugin.acp.acp_connection import ACPConnection
from plugin.framework.async_stream import StreamQueueKind
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
# tool_call_update statuses that carry a finished invocation. in_progress
# is still tool activity, but it is not a result yet.
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})
# shutil.which on Windows returns the PATHEXT hit (hermes.exe, hermes.cmd,
# hermes.bat). Those stems are the same CLI as the POSIX basename.
_WINDOWS_CLI_SUFFIXES = frozenset({".exe", ".cmd", ".bat"})


def _split_cli_args(args_str: str, *, posix: bool) -> list[str]:
    """Split settings args. Windows keeps backslashes, then drops wrapping quotes.

    ``posix=True`` treats ``\\`` as an escape, so ``C:\\new\\tmp`` becomes
    ``C:newtmp``. ``posix=False`` keeps the backslashes and also keeps the
    quote characters, so ``Popen`` would look for a file whose name contains
    a literal quote.
    """
    tokens = shlex.split(args_str, posix=posix)
    if posix:
        return tokens
    stripped: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
            stripped.append(token[1:-1])
        else:
            stripped.append(token)
    return stripped


def _directory_from_document_url(document_url: str | None) -> str | None:
    """Parent directory of a ``file://`` document, or None when it is not a local file."""
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
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
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
# Stop sends session/cancel and keeps the CLI. If the prompt has not
# returned by then, the agent ignored cancel and the tree is killed.
_CANCEL_GRACE_SEC = 3.0


class _TurnStopped(Exception):
    """This turn was stopped before its JSON-RPC request was sent."""


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
    """``TOOL_RESULT`` only for a finished call.

    What was wrong: any update that carried ``content`` or ``rawOutput``
    was a result, including ``status: in_progress``. A streaming tool
    then showed several "[Tool result]" lines for one call. The status
    comment already said in_progress is not a result. Why: pending and
    in_progress stay activity. A terminal status, or content with no
    status, is the result.
    """
    if update.get("sessionUpdate") != "tool_call_update":
        return StreamQueueKind.TOOL_CALL
    status = update.get("status")
    if status in ("pending", "in_progress"):
        return StreamQueueKind.TOOL_CALL
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
      copies it onto ``_extra_args`` when ``_binary_matches`` is true.
      The base match is the basename, including ``.exe`` / ``.cmd`` / ``.bat``.
      Grok overrides ``_binary_matches`` for a ``grok`` prefix.
    """

    default_extra_args: Tuple[str, ...] = ()
    _ctx: Any | None
    _stop_requested: bool
    _state_lock: threading.Lock
    _pending_permissions: dict[Any, list[Any]]
    _turn: int
    _turn_pending: bool
    _stop_turn: int
    _ready: bool
    _session_url: str | None
    _session_prompted: bool
    _prompt_inflight: bool
    _prompt_done: threading.Event

    def __init__(self, ctx: Any | None = None) -> None:
        self._ctx = ctx
        self._conn: ACPConnection | None = None
        self._session_id: str | None = None
        self._session_url = None
        self._session_prompted = False
        self._ready = False
        self._stop_requested = False
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
                # An empty setting means "search PATH". A path that is not a
                # file is a typo, and falling through with no log hid it.
                if path:
                    log.warning("ACP agent_backend.path is not a file (%s); searching PATH", path)
                self._binary_path = self._find_binary()

            args_str = str(get_config("agent_backend.args") or "").strip()
            # str.split() breaks a quoted path into several argv entries.
            # posix=False on Windows so backslashes in paths stay literal.
            self._extra_args = _split_cli_args(args_str, posix=(os.name != "nt")) if args_str else []
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
        if self._binary_matches(self._binary_path):
            self._extra_args = list(defaults)

    def _binary_matches(self, path: str) -> bool:
        """True when ``path`` is this backend's CLI, including Windows launcher suffixes."""
        return _cli_basename_matches(path, self.get_binary_name())

    def _find_binary(self) -> str | None:
        """Find the binary in PATH or install dirs a short desktop PATH misses.

        A GUI-launched LibreOffice often has ``/usr/bin:/bin`` only. npm,
        nvm, Homebrew, and OpenCode installs live outside that. One list
        serves every backend: the file name is the discriminant.
        """
        binary_name = self.get_binary_name()
        found = shutil.which(binary_name)
        if found:
            return found
        for candidate in _binary_candidates(binary_name):
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
        # _load_config already ran _find_binary, which searches PATH and the
        # home install dirs. A second which() only ran when isfile() disagreed.
        self._load_config()
        if self._binary_path:
            log.info("%s binary found: %s", self.get_display_name(), self._binary_path)
            return True
        log.info("%s binary not found", self.get_display_name())
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
        """Start the ACP subprocess, or keep the one this sidebar already has.

        A live initialized child is the slot. A live child that never
        finished ``initialize`` (Stop during the startup wait) is reused
        for the handshake. A dead child is dropped here, not left on the slot.
        """
        existing = self._conn
        if existing is not None and existing.is_alive and self._ready:
            return
        if existing is not None and existing.is_alive:
            conn = existing
        else:
            if existing is not None:
                self.shutdown()
            if self._stop_requested:
                return
            if not self._binary_path:
                raise RuntimeError(f"{self.get_display_name()} binary not found. Install {self.get_binary_name()} and ensure it's in PATH.")

            cmd_line = [self._binary_path]
            cmd_line.extend(self._extra_args)

            # ACPConnection.start merges os.environ itself. Passing a full copy
            # here merged the process environment twice.
            conn = ACPConnection(cmd_line=cmd_line, env=self.get_env_vars() or None)
            self._conn = conn
            self._ready = False
            conn.start()

            # What was wrong: a half-second poll after every spawn delayed the
            # first message so an instant crash could be named before
            # initialize, and so Stop during that sleep skipped the handshake.
            # Why: initialize fails on its own when the reader wakes the
            # waiter. One liveness check still reports a child that is already
            # dead, with its stderr. A Stop that landed during spawn is
            # honored here, before the request is sent. Keep this local:
            # shutdown can set self._conn to None.
            if self._startup_stopped(stop_checker):
                return
            if not conn.is_alive:
                detail = conn.stderr_text().strip()
                message = f"{self.get_display_name()} ACP process failed to start."
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
                15,
            )
            self._ready = True
            log.info(f"ACP initialized: {result}")
        except _TurnStopped:
            # Stop won before initialize was sent. Leave the child so the
            # next message can finish the handshake. _ready stays false.
            return
        except Exception as exc:
            log.exception("ACP initialize failed")
            # A crash during the handshake used to say only "process
            # terminated". The stderr tail is the CLI's own reason.
            detail = conn.stderr_text().strip() if not conn.is_alive else ""
            self._release_connection(conn)
            if detail:
                raise RuntimeError(f"{self.get_display_name()} ACP process failed to start. {detail[:500]}") from exc
            raise

    def _ensure_session(self, mcp_url: str | None = None, document_url: str | None = None) -> None:
        """Open an ACP session for this document, or keep the one it already has.

        The same document URL reuses ``sessionId``. A different URL, including
        the first save, starts another session on this process.
        """
        url_key = document_url or ""
        if self._session_id and self._session_url == url_key:
            return
        # What was wrong: ``if self._conn`` then ``self._conn.send_request``
        # loaded the connection twice. stop() sets it to None between those
        # loads. A missing connection also returned without a session id, and
        # the later check reported Stopped. Why: one local, and raise when
        # there is no process.
        conn = self._conn
        if conn is None:
            raise RuntimeError(f"{self.get_display_name()} ACP process is not running.")

        # mcp_servers is required by the ACP schema
        # Attach mcp_servers only when MCP is enabled. A disabled server must
        # not be handed to session/new.
        from plugin.framework.config import get_config
        from plugin.framework.config_schema import as_bool

        mcp_servers = []
        if mcp_url and as_bool(get_config("mcp.mcp_enabled")):
            mcp_servers.append({"url": mcp_url, "name": "writeragent", "type": "http", "headers": []})

        params = {"cwd": _session_cwd(document_url), "mcpServers": mcp_servers}

        try:
            result = self._request(conn, "session/new", params, 30)
            session_id = result.get("sessionId") if isinstance(result, dict) else None
            # An empty id used to be stored and sent on session/prompt.
            if not isinstance(session_id, str) or not session_id.strip():
                raise RuntimeError(f"{self.get_display_name()} ACP session/new returned no sessionId.")
            self._session_id = session_id
            self._session_url = url_key
            self._session_prompted = False
            log.debug(f"ACP session created: {self._session_id}")
        except _TurnStopped:
            # Stop won before session/new was sent. Not a session failure,
            # and the process is still the one the next message should use.
            raise
        except Exception:
            log.exception("ACP session creation failed")
            # The process can retry session/new. Drop it only when it has died.
            if self._conn is conn and not conn.is_alive:
                self.shutdown()
            raise

    def _build_prompt_blocks(self, user_message: str, document_context: Optional[str] = None, system_prompt: Optional[str] = None, selection_text: Optional[str] = None, document_url: Optional[str] = None, *, include_system: bool = True) -> list[dict[str, Any]]:
        """Build ACP prompt content blocks.

        The first prompt of a session carries the manual. Later prompts do
        not: the CLI already has it. The document snapshot is sent every
        time, because the file changes between messages.
        """
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")

        if is_slash_command:
            # For slash commands, only send the command itself
            prompt_blocks.append({"type": "text", "text": user_message})
        else:
            if include_system and system_prompt:
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
        elif msg_id is not None:
            # session/update is a notification and usually has no id. One
            # sent as a request was marked handled and never answered, so
            # the agent blocked. An empty result unblocks it.
            self._ack_request(msg_id)

    def _reply(self, msg_id: Any, *, result: Any = None, error: Any = None, what: str) -> None:
        """Send one JSON-RPC response. A missing connection is a no-op."""
        conn = self._conn
        if conn is None:
            return
        try:
            if error is not None:
                conn.send_response(msg_id, error=error)
            else:
                conn.send_response(msg_id, result=result if result is not None else {})
        except Exception:
            log.exception("Failed to %s", what)

    def _reject_unknown_request(self, msg_id: Any, method: str) -> None:
        self._reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"}, what=f"reject ACP method {method}")

    def _ack_request(self, msg_id: Any) -> None:
        self._reply(msg_id, result={}, what=f"acknowledge ACP request {msg_id}")

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
        with self._state_lock:
            if self._stop_requested:
                reply_cancelled = msg_id is not None
            else:
                reply_cancelled = False
                if msg_id is not None:
                    self._pending_permissions[msg_id] = stored
                else:
                    log.warning("ACP permission request missing id; cannot answer it")
        if reply_cancelled:
            self._reply(msg_id, result=_cancelled_permission(), what=f"cancel ACP permission {msg_id}")
            return
        description = _permission_description(params)
        tool_name = _permission_tool_name(tool_call)
        queue.put((StreamQueueKind.APPROVAL_REQUIRED, description, tool_name, tool_call, msg_id))

    def _pop_pending_permission_ids(self) -> list[Any]:
        """Drop the pending map. Caller holds ``_state_lock``."""
        pending_ids = list(self._pending_permissions)
        self._pending_permissions.clear()
        return pending_ids

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
        self._session_prompted = False

    def _cancel_pending_permissions(self) -> None:
        """Answer every outstanding permission with the ACP ``cancelled`` outcome.

        Stop must reply, or the agent blocks inside
        ``session/request_permission`` and keeps the turn alive. The spec
        requires ``cancelled`` when the client sends ``session/cancel``.
        Pop under the lock so a concurrent Approve cannot answer twice.
        """
        with self._state_lock:
            pending_ids = self._pop_pending_permission_ids()
        for request_id in pending_ids:
            self._reply(request_id, result=_cancelled_permission(), what=f"cancel ACP permission {request_id}")

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

        What was wrong: the first ``send()`` with no ``prepare_turn`` set
        the published id equal to ``_turn``. A Stop on that turn then
        matched every later send, which returned Stopped forever. Why:
        ``prepare_turn`` publishes an id that this send consumes. A send
        that was not published opens a new turn and drops the previous
        Stop. Only a Stop for the consumed id, or a true ``stop_checker``,
        aborts.
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
        """``send_request`` marked in flight so Stop can bound it.

        What was wrong: ``send()`` checked ``_stop_requested``, then this set
        ``_prompt_inflight``. ``stop()`` in that gap saw inflight false, so
        it sent no ``session/cancel`` and armed no grace. The prompt ran to
        completion and the UI only said Stopped afterwards. Why: the stop
        latch and the inflight flag share ``_state_lock``. A Stop that
        already won this turn never reaches ``send_request``. The fresh
        adapter has ``_stop_turn == _turn == 0`` with the latch clear, so
        the latch has to be set too: equality alone is the unset pair.
        """
        with self._state_lock:
            if self._stop_requested and self._stop_turn == self._turn:
                raise _TurnStopped()
            self._prompt_done.clear()
            self._prompt_inflight = True
        try:
            return conn.send_request(method, params, timeout=timeout)
        finally:
            with self._state_lock:
                self._prompt_inflight = False
            self._prompt_done.set()

    def _arm_cancel_grace(self, turn: int) -> None:
        """Kill the CLI if this turn's request is still waiting after the grace."""

        def _watch() -> None:
            # Activity after Stop does not extend this. The idle prompt
            # clock would, and a wedged agent would keep editing.
            if self._prompt_done.wait(_CANCEL_GRACE_SEC):
                return
            self._kill_turn_if_still_inflight(turn)

        run_in_background(_watch, name="acp-cancel-grace", dedicated=True)

    def _kill_turn_if_still_inflight(self, turn: int) -> None:
        """Stop the CLI captured for ``turn`` when that request is still in flight.

        What was wrong: the turn check and ``shutdown()`` were not one
        critical section. ``prepare_turn`` could publish the next turn, and
        that send could adopt the same child, before ``shutdown()`` ran.
        Why: the slot is cleared under the same lock as the check. The
        captured connection is the one that is stopped. ``shutdown()`` reads
        ``self._conn``, so calling it after the slot is cleared would skip
        the kill. The lock is not held across ``stop()``.
        """
        with self._state_lock:
            if self._turn != turn or not self._prompt_inflight:
                return
            conn = self._conn
            pending_ids = self._pop_pending_permission_ids()
            self._clear_slot_locked(conn)
        log.info("ACP cancel was not answered within %ss; killing the CLI", _CANCEL_GRACE_SEC)
        for request_id in pending_ids:
            if conn is None:
                break
            try:
                conn.send_response(request_id, result=_cancelled_permission())
            except Exception:
                log.exception("Failed to cancel ACP permission %s", request_id)
        if conn is None:
            return
        try:
            conn.set_notification_callback(None)
        except Exception:
            log.exception("Failed to clear ACP notification callback")
        try:
            conn.stop()
        except Exception:
            log.exception("ACP shutdown failed")

    def send(self, queue: Any, user_message: str, document_context: str | None, document_url: str | None, system_prompt: str | None = None, mcp_url: str | None = None, selection_text: str | None = None, stop_checker: Any = None, **kwargs: Any) -> None:
        """Send one prompt. The CLI stays up for the next message on this sidebar."""
        # ``register_on_cancel(adapter.stop)`` can run before this worker
        # enters ``send()``. Clearing ``_stop_requested`` on entry, and never
        # reading ``stop_checker``, wiped that latch. The UI showed Stopped
        # while the CLI, session, and prompt still started. Under the same
        # lock ``stop()`` sets, honor a Stop for this turn or a true
        # ``stop_checker`` and return without starting a new process.
        if self._turn_already_stopped(stop_checker):
            self._finish_stopped(queue)
            return

        queue.put((StreamQueueKind.STATUS, f"Starting {self.get_display_name()}..."))

        try:
            try:
                self._ensure_connection(stop_checker)
            except Exception as e:
                self._report_failure(queue, format_error_payload(RuntimeError(f"Cannot start {self.get_display_name()} ACP. Is {self.get_binary_name()} installed? Error: {e}")))
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
            conn = self._conn
            if conn is not None:
                conn.set_notification_callback(on_notification)

            try:
                self._ensure_session(mcp_url=mcp_url, document_url=document_url)
            except Exception as e:
                self._report_failure(queue, format_error_payload(RuntimeError(f"Session creation failed: {e}")))
                return

            if self._stop_requested:
                self._finish_stopped(queue)
                return

            queue.put((StreamQueueKind.STATUS, f"Sending to {self.get_display_name()}..."))

            prompt_blocks = self._build_prompt_blocks(user_message=user_message, document_context=document_context, system_prompt=system_prompt, selection_text=selection_text, document_url=document_url, include_system=not self._session_prompted)

            try:
                # What was wrong: ``not self._conn`` then ``self._conn.send_request``
                # loaded the connection twice. stop() sets it to None in
                # between, and None.send_request raised AttributeError, which
                # the generic handler reported as Stopped. A missing process
                # with no Stop took that same Stopped path. Why: one local.
                # No process and no Stop is an error.
                conn = self._conn
                if self._stop_requested:
                    self._finish_stopped(queue)
                    return
                if conn is None:
                    self._report_failure(queue, format_error_payload(RuntimeError(f"{self.get_display_name()} ACP process is not running.")))
                    return

                result = self._request(conn, "session/prompt", {"sessionId": self._session_id, "prompt": prompt_blocks}, _PROMPT_IDLE_SEC)
                self._session_prompted = True

                if self._stop_requested:
                    self._finish_stopped(queue)
                    return

                self._finish_prompt_result(result, queue)

            except _TurnStopped:
                self._finish_stopped(queue)
            except TimeoutError:
                # No accepted stdout for the idle window. The CLI is wedged.
                self._report_failure(queue, format_error_payload(RuntimeError(f"{self.get_display_name()} prompt timed out")))
                if not self._stop_requested:
                    self.shutdown()
            except Exception as e:
                if self._stop_requested:
                    self._finish_stopped(queue)
                else:
                    log.exception("Prompt execution failed")
                    self._report_failure(queue, format_error_payload(e))
                    # The exception can land before ``conn`` is known to be
                    # live. A dead child is dropped. A JSON-RPC error on a
                    # live child keeps the process for the next message.
                    if conn is not None and not conn.is_alive:
                        self.shutdown()
        finally:
            # The process stays. Drop the callback so a late notification
            # does not write into this turn's queue. The next send installs
            # its own. shutdown() may already have cleared the connection.
            conn = self._conn
            if conn is not None:
                try:
                    conn.set_notification_callback(None)
                except Exception:
                    log.exception("Failed to clear ACP notification callback")

    def _finish_prompt_result(self, result: Any, queue: Any) -> None:
        """Queue the terminal event for a ``session/prompt`` result.

        What was wrong: every result, including ``refusal``, was logged and
        then ``STREAM_DONE``. An empty refusal looked like a successful
        answer. A non-dict result raised ``AttributeError`` on ``.get``.
        Why: ACP says a refusal should show in the UI. A token or turn
        limit still keeps the partial answer: the chat drain's error path
        stores only the banner and drops text already streamed.
        """
        if not isinstance(result, dict):
            if result:
                queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.get_display_name()} returned an unexpected prompt result."))))
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
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.get_display_name()} refused the prompt."))))
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
            turn = self._turn
        conn = self._conn
        if inflight and conn is not None and conn.is_alive and self._session_id:
            try:
                conn.send_notification("session/cancel", {"sessionId": self._session_id})
            except Exception:
                log.exception("ACP session/cancel failed")
        # Reply while stdin is still open. A later shutdown closes it.
        self._cancel_pending_permissions()
        if inflight:
            self._arm_cancel_grace(turn)

    def submit_approval(self, request_id: Any, approved: bool) -> None:
        """Submit a schema-valid permission result for one HITL dialog choice.

        Agents reject ``{"approved": bool}``. ACP requires
        ``outcome.selected`` plus an ``optionId`` from the request, or
        ``outcome.cancelled``. The options were stored when the request
        arrived so this can echo one of those ids.
        """
        conn = self._conn
        if conn is None or not conn.is_alive:
            log.warning("Cannot submit approval, ACP connection is dead")
            return

        with self._state_lock:
            options = self._pending_permissions.pop(request_id, None)
        if options is None:
            # Stop already answered with cancelled, or the id is unknown.
            log.warning("No pending ACP permission for id=%s", request_id)
            return

        self._reply(request_id, result=_permission_result(options, approved), what="submit ACP approval")

    def shutdown(self) -> None:
        """Kill the CLI and drop the session.

        The sidebar calls this when the panel closes or the backend id
        changes. A cancel that the agent does not answer, a silent prompt,
        and a dead child call it too. A finished ``send`` does not.
        """
        conn = self._conn
        self._cancel_pending_permissions()
        self._release_connection(conn)

    def _release_connection(self, conn: ACPConnection | None) -> None:
        """Reap ``conn`` and forget its session when it is still the slot.

        What was wrong: ``_ready`` and the session fields were cleared
        even after ``self._conn`` had moved to another process, and the
        write sat outside ``_state_lock`` while the cancel grace wrote
        the same fields under it. Why: stop the captured process first.
        Under the lock, drop the slot only when it is still this
        connection. A replacement keeps its session.
        """
        if conn is not None:
            try:
                conn.set_notification_callback(None)
            except Exception:
                log.exception("Failed to clear ACP notification callback")
            try:
                conn.stop()
            except Exception:
                log.exception("ACP shutdown failed")
        with self._state_lock:
            self._clear_slot_locked(conn)
