"""Hermes agent backend adapter. Keeps one long-lived Hermes CLI process; each Send writes to
stdin and streams the next response. Conversation context is preserved across messages.

We never close stdin (or the PTY write side)—closing it would signal EOF and cause Hermes to exit.
Only stop() terminates the process; between messages the process stays running with stdin open.

Expects Hermes to be configured with WriterAgent's MCP server in ~/.hermes/config.yaml.
"""

import os
import re
import shlex
import shutil
import struct
import subprocess
import threading
import time

try:
    import pty
    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False

try:
    import fcntl
    import termios
    _WINSIZE_AVAILABLE = True
except ImportError:
    _WINSIZE_AVAILABLE = False

from plugin.modules.agent_backend.base import AgentBackend
from plugin.framework.logging import debug_log

_LOG = "Hermes"


# ANSI strip
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;.*?\x1b\\")
_HERMES_PROMPT_CHAR = "\u276f"


def _strip_ansi(text):
    if not text:
        return text
    return _ANSI_RE.sub("", text)


def _is_hermes_prompt(line):
    if not line or _HERMES_PROMPT_CHAR not in line:
        return False
    s = _strip_ansi(line).strip()
    s = re.sub(r"[\s\u2500-\u257f\-]+", "", s)
    return s == _HERMES_PROMPT_CHAR


# ── Shared long-lived process state (one Hermes per WriterAgent run) ──
_lock = threading.Lock()
_process = None
_pty_master_write = None  # when using PTY, send() writes here (child stdin)
_reader_thread = None
_reader_ready = threading.Event()
_current_queue = None
_response_done = threading.Event()
_stop_requested = False
_stderr_lines = []  # last N stderr lines for diagnostics when process exits/hangs


def _stderr_drain_loop(proc):
    """Drain stderr so Hermes never blocks on a full stderr pipe (can cause deadlock)."""
    global _stderr_lines
    try:
        for line in iter(proc.stderr.readline, ""):
            line = _strip_ansi(line).strip()
            if line:
                debug_log("hermes stderr: %s" % line[:200], context=_LOG)
                _stderr_lines.append(line[:300])
                if len(_stderr_lines) > 50:
                    _stderr_lines.pop(0)
    except Exception:
        pass


def _reader_loop(stdout_stream):
    """Run in background; pushes to _current_queue when set, sets _reader_ready on first ❯.
    stdout_stream: file-like with readline() (either proc.stdout or pty master).
    """
    global _current_queue, _reader_ready, _response_done, _stop_requested
    past_banner = False
    line_count = [0]  # list so we can mutate from nested scope
    response_chunk_count = [0]
    debug_log("reader_loop: started", context=_LOG)
    try:
        for line in iter(stdout_stream.readline, ""):
            if _stop_requested:
                debug_log("reader_loop: stop_requested, exiting", context=_LOG)
                break
            line_count[0] += 1
            raw_line = line
            line = _strip_ansi(line)
            if not line:
                if raw_line == "":
                    debug_log("reader_loop: readline returned empty (EOF), process may have exited, line_count=%d" % line_count[0], context=_LOG)
                continue
            preview = repr((line[:50] + "…") if len(line) > 50 else line)
            if _current_queue is None:
                # Waiting for first prompt or between messages
                if _is_hermes_prompt(line):
                    past_banner = True
                    _reader_ready.set()
                    debug_log("reader_loop: saw prompt (❯), _reader_ready set (between messages)", context=_LOG)
                elif line_count[0] <= 20 or line_count[0] % 50 == 0:
                    debug_log("reader_loop: skip line #%d (no queue) %s" % (line_count[0], preview), context=_LOG)
                continue
            # In a response: push chunks until we see the next ❯
            if _is_hermes_prompt(line) or "Goodbye" in line:
                debug_log("reader_loop: saw end prompt / Goodbye, pushing stream_done (chunks pushed this response=%d)" % response_chunk_count[0], context=_LOG)
                _current_queue.put(("stream_done", None))
                _current_queue = None
                _response_done.set()
                response_chunk_count[0] = 0
                continue
            response_chunk_count[0] += 1
            if response_chunk_count[0] <= 3 or response_chunk_count[0] % 100 == 0:
                debug_log("reader_loop: response chunk #%d %s" % (response_chunk_count[0], preview), context=_LOG)
            _current_queue.put(("chunk", line if line.endswith("\n") else line + "\n"))
    except (OSError, IOError) as e:
        if getattr(e, "errno", None) == 5:
            proc = None
            try:
                with _lock:
                    proc = _process
            except Exception:
                pass
            alive = proc is not None and proc.poll() is None
            stderr_snippet = ("; ".join(_stderr_lines[-5:])) if _stderr_lines else ""
            debug_log("reader_loop: EIO (errno 5) - process_alive=%s returncode=%s; stderr tail: %s"
                      % (alive, getattr(proc, "returncode", None) if proc else None, stderr_snippet[:200]), context=_LOG)
            if _current_queue is not None:
                msg = (
                    "Hermes subprocess ended unexpectedly (I/O error). "
                    "Start the MCP server first (WriterAgent → Toggle MCP Server), then try again."
                )
                _current_queue.put(("error", RuntimeError(msg)))
        else:
            debug_log("reader_loop: exception %s" % e, context=_LOG)
            if _current_queue is not None:
                _current_queue.put(("error", e))
    except Exception as e:
        debug_log("reader_loop: exception %s" % e, context=_LOG)
        if _current_queue is not None:
            _current_queue.put(("error", e))
    finally:
        debug_log("reader_loop: exiting (total lines read=%d), setting _response_done" % line_count[0], context=_LOG)
        _response_done.set()
        _current_queue = None


def _ensure_process(path, args_str, queue, stop_checker):
    """Start process and reader if not running. Return (proc, True) or (None, False).
    Uses a PTY when available so Hermes sees a terminal and line-buffers output (fixes second-message hang).
    """
    global _process, _reader_thread, _reader_ready, _current_queue, _response_done, _pty_master_write
    with _lock:
        if _process is not None and _process.poll() is None:
            debug_log("ensure_process: reusing existing process (pid=%s)" % getattr(_process, "pid", None), context=_LOG)
            return _process, True
        debug_log("ensure_process: no live process, starting new one", context=_LOG)
        _reader_ready.clear()
        _current_queue = None
        _response_done.clear()
        _stderr_lines[:] = []
        if _pty_master_write is not None:
            try:
                _pty_master_write.close()
            except Exception:
                pass
            _pty_master_write = None
        base_cmd = [path if path else "hermes"]
        if args_str:
            base_cmd.extend(shlex.split(args_str))
        use_pty = _PTY_AVAILABLE and os.name != "nt"
        if use_pty:
            master_read = None
            try:
                master_fd, slave_fd = pty.openpty()
                # Set a sane terminal size so Hermes (and libs like readline) don't exit or hang on 0x0
                if _WINSIZE_AVAILABLE:
                    try:
                        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                    except Exception as e:
                        debug_log("ensure_process: set PTY winsize failed %s (continuing)" % e, context=_LOG)
                # Two streams (read / write) avoid "r+" which can raise "not seekable" in some environments
                master_read_fd = os.dup(master_fd)
                master_read = open(master_read_fd, "r", encoding="utf-8", errors="replace", newline="\n")
                _pty_master_write = open(master_fd, "w", encoding="utf-8", errors="replace", newline="\n")
                try:
                    _process = subprocess.Popen(
                        base_cmd,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=subprocess.PIPE,
                        env=os.environ.copy(),
                        start_new_session=True,  # own session: avoid SIGTERM from LO/parent
                    )
                    os.close(slave_fd)
                    slave_fd = None
                except Exception:
                    os.close(slave_fd)
                    raise
                stdout_stream = master_read
                debug_log("ensure_process: Popen with PTY ok, pid=%s (Hermes will see a TTY)" % _process.pid, context=_LOG)
            except Exception as e:
                debug_log("ensure_process: PTY spawn failed %s, falling back to pipes" % e, context=_LOG)
                if master_read is not None:
                    try:
                        master_read.close()
                    except Exception:
                        pass
                if _pty_master_write is not None:
                    try:
                        _pty_master_write.close()
                    except Exception:
                        pass
                    _pty_master_write = None
                use_pty = False
        if not use_pty:
            cmd = base_cmd
            if shutil.which("stdbuf"):
                cmd = ["stdbuf", "-o", "L"] + base_cmd
            debug_log("ensure_process: using pipes, cmd=%s" % cmd, context=_LOG)
            try:
                _process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=os.environ.copy(),
                    bufsize=1,
                    start_new_session=True,  # own session: avoid SIGTERM from LO/parent
                )
            except FileNotFoundError as e:
                debug_log("ensure_process: FileNotFoundError %s" % e, context=_LOG)
                return None, False
            except Exception as e:
                debug_log("ensure_process: Popen failed %s" % e, context=_LOG)
                return None, False
            stdout_stream = _process.stdout
        try:
            _reader_thread = threading.Thread(target=_reader_loop, args=(stdout_stream,), daemon=True)
            _reader_thread.start()
            _stderr_thread = threading.Thread(target=_stderr_drain_loop, args=(_process,), daemon=True)
            _stderr_thread.start()
        except Exception as e:
            debug_log("ensure_process: failed to start reader/stderr %s" % e, context=_LOG)
            if _process:
                try:
                    _process.terminate()
                except Exception:
                    pass
            _process = None
            return None, False
        debug_log("ensure_process: process and reader started, returning (no wait for ❯)", context=_LOG)
    return _process, True


class HermesBackend(AgentBackend):
    backend_id = "hermes"
    display_name = "Hermes"

    def __init__(self, ctx=None):
        self._ctx = ctx

    def is_available(self, ctx):
        try:
            from plugin.framework.config import get_config
            path = str(get_config(ctx, "agent_backend.path", "") or "").strip()
            if path:
                return os.path.isfile(path) or bool(shutil.which(path))
            return bool(shutil.which("hermes"))
        except Exception:
            pass
        return False

    def stop(self):
        global _stop_requested, _process
        _stop_requested = True
        with _lock:
            proc = _process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        _response_done.set()

    def send(
        self,
        queue,
        user_message,
        document_context,
        document_url,
        system_prompt=None,
        selection_text=None,
        stop_checker=None,
        **kwargs
    ):
        global _stop_requested, _current_queue, _response_done, _process
        _stop_requested = False

        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "agent_backend.path", "") or "").strip()
            args_str = str(get_config(self._ctx, "agent_backend.args", "") or "").strip()
        except Exception:
            path = ""
            args_str = ""

        parts = []
        if document_context:
            parts.append("Document context:\n\n")
            parts.append(document_context)
            parts.append("\n\n")
        if system_prompt:
            parts.append("Instructions:\n\n")
            parts.append(system_prompt)
            parts.append("\n\n")
        parts.append("User: ")
        parts.append(user_message)
        parts.append("\n")
        stdin_payload = "".join(parts)

        with _lock:
            need_start = _process is None or (_process and _process.poll() is not None)
        debug_log("send(): entry, path=%r, need_start=%s" % (path or "hermes", need_start), context=_LOG)
        queue.put(("status", "Starting Hermes..." if need_start else "Sending..."))

        proc, ok = _ensure_process(path, args_str, queue, stop_checker)
        if not ok:
            debug_log("send(): _ensure_process returned not ok, proc=%s" % proc, context=_LOG)
            if proc is None:
                queue.put((
                    "error",
                    RuntimeError(
                        "Hermes not found. Install it or set Settings → Agent backends → Path."
                    ),
                ))
            else:
                queue.put(("error", RuntimeError("Hermes did not show ready prompt within 30s.")))
            return

        queue.put(("status", "Sending to Hermes..."))
        debug_log("send(): process ready, pid=%s, writing payload (%d bytes)" % (getattr(proc, "pid", None), len(stdin_payload)), context=_LOG)
        _response_done.clear()
        _current_queue = queue
        debug_log("send(): _current_queue set, reader will push chunks when stdout has data", context=_LOG)
        try:
            # When using PTY we write to the master write stream; otherwise to proc.stdin.
            # We never close stdin/pty so Hermes sees an open terminal and stays running.
            stdin_stream = _pty_master_write if _pty_master_write is not None else proc.stdin
            stdin_stream.write(stdin_payload)
            stdin_stream.flush()
            debug_log("send(): payload written, waiting for _response_done", context=_LOG)
        except Exception as e:
            _current_queue = None
            debug_log("send(): write failed %s" % e, context=_LOG)
            queue.put(("error", e))
            return

        timeout_seconds = 300
        deadline = time.monotonic() + timeout_seconds
        last_log = [time.monotonic()]
        mcp_hint_shown = [False]
        while not _response_done.is_set() and time.monotonic() < deadline:
            if _stop_requested or (stop_checker and stop_checker()):
                debug_log("send(): stop requested while waiting", context=_LOG)
                break
            now = time.monotonic()
            elapsed = now - (deadline - timeout_seconds)
            if now - last_log[0] >= 5.0:
                debug_log("send(): still waiting for _response_done, proc.alive=%s, elapsed=%.1fs" % (
                    proc.poll() is None, elapsed), context=_LOG)
                if elapsed >= 15.0 and not mcp_hint_shown[0]:
                    queue.put(("status", "Waiting for Hermes… (ensure MCP server is running: WriterAgent → Toggle MCP Server)"))
                    mcp_hint_shown[0] = True
                last_log[0] = now
            _response_done.wait(timeout=0.25)

        _current_queue = None
        elapsed = time.monotonic() - (deadline - timeout_seconds)
        debug_log("send(): done waiting, stopped=%s returncode=%s elapsed=%.1fs" % (
            _stop_requested, getattr(proc, "returncode", None), elapsed), context=_LOG)

        # Prefer showing process exit error over "stopped" so user sees why Hermes died
        if proc.poll() is not None and proc.returncode != 0:
            try:
                err = proc.stderr.read() if proc.stderr else ""
                err = _strip_ansi(err).strip()
            except Exception:
                err = ""
            if not err and _stderr_lines:
                err = "; ".join(_stderr_lines[-8:])
            if not err:
                err = "Hermes exited with code %s. Start the MCP server first (WriterAgent → Toggle MCP Server)." % proc.returncode
            debug_log("send(): process exited, returncode=%s, stderr: %s" % (proc.returncode, err[:300]), context=_LOG)
            queue.put(("error", RuntimeError(err)))
        elif _stop_requested or (stop_checker and stop_checker()):
            queue.put(("stopped",))
        else:
            queue.put(("stream_done", None))
