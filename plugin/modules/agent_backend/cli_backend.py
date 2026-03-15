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
"""Base class for CLI-based agent backends (Hermes, Aider, OpenHands, etc.).
Extracts the long-lived process management, PTY handling, threading, and queueing logic.
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

# CSI: ESC [ ... letter. Allow optional spaces (e.g. \x1b[2 q) and broader param chars.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;? ]*[a-zA-Z]")
# OSC and similar: ESC ] ... ESC \
_ANSI_OSC_RE = re.compile(r"\x1b\][0-9]*;.*?\x1b\\")
# Fallback: any ESC [ ... up to next letter (catches CPR and other controls)
_ANSI_CSI_LOOSE_RE = re.compile(r"\x1b\[[^\x00-\x1f\x7f]*[a-zA-Z@`{|]")

def strip_ansi(text):
    if not text:
        return text
    t = _ANSI_CSI_RE.sub("", text)
    t = _ANSI_OSC_RE.sub("", t)
    t = _ANSI_CSI_LOOSE_RE.sub("", t)
    return t


class CLIProcessBackend(AgentBackend):
    """A generic long-lived CLI process backend.

    Subclasses should define:
    - backend_id, display_name
    - get_default_cmd(): default executable name
    - is_ready_prompt(line): return True if line indicates the agent is ready for input.
    - is_end_of_response(line): return True if line indicates the agent has finished its response.
    - format_input(user_message, document_context, ...): return a string to send to stdin.
    """

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._lock = threading.Lock()
        self._process = None
        self._pty_master_write = None
        self._reader_thread = None
        self._reader_ready = threading.Event()
        self._current_queue = None
        self._response_done = threading.Event()
        self._stop_requested = False
        self._stderr_lines = []
        self._log_prefix = self.__class__.__name__

    def is_available(self, ctx):
        try:
            from plugin.framework.config import get_config
            path = str(get_config(ctx, "agent_backend.path") or "").strip()
            if path:
                return os.path.isfile(path) or bool(shutil.which(path))
            cmd = self.get_default_cmd()
            if not cmd:
                return False
            # Split to get just the executable name for which()
            exe = shlex.split(cmd)[0]
            return bool(shutil.which(exe))
        except Exception:
            pass
        return False

    def get_default_cmd(self):
        """Return the default command name (e.g., 'hermes', 'aider')."""
        raise NotImplementedError

    def is_ready_prompt(self, line):
        """Return True if the line indicates the CLI is ready for new input."""
        raise NotImplementedError

    def is_end_of_response(self, line):
        """Return True if the line indicates the CLI has finished responding to the current input."""
        raise NotImplementedError

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, mcp_url=None, **kwargs):
        """Return the string payload to write to stdin."""
        raise NotImplementedError

    def should_forward_chunk(self, line):
        """Return True if this line should be forwarded to the UI as response content. Subclasses may override to filter banner/echo lines."""
        return True

    def _stderr_drain_loop(self, proc):
        """Drain stderr so process never blocks on a full stderr pipe."""
        try:
            for line in iter(proc.stderr.readline, ""):
                line = strip_ansi(line).strip()
                if line:
                    debug_log(f"{self._log_prefix} stderr: {line[:200]}", context=self._log_prefix)
                    self._stderr_lines.append(line[:300])
                    if len(self._stderr_lines) > 50:
                        self._stderr_lines.pop(0)
        except Exception:
            pass

    def _process_line(self, line, line_count, response_chunk_count):
        raw_line = line
        line = strip_ansi(line)
        if not line:
            if raw_line == "":
                debug_log(f"reader_loop: read empty (EOF), process may have exited, line_count={line_count[0]}", context=self._log_prefix)
            return

        preview = repr((line[:50] + "…") if len(line) > 50 else line)

        if self._current_queue is None:
            if self.is_ready_prompt(line):
                self._reader_ready.set()
                debug_log(f"reader_loop: saw prompt, _reader_ready set (between messages) {preview}", context=self._log_prefix)
            elif line_count[0] <= 20 or line_count[0] % 50 == 0:
                debug_log(f"reader_loop: skip line #{line_count[0]} (no queue) {preview}", context=self._log_prefix)
            return

        if self.is_end_of_response(line):
            # Only count as 'end of response' if we were currently waiting for one.
            # If we were already in 'between turns' state, this prompt is just confirming readiness.
            if self._current_queue is not None:
                debug_log(f"reader_loop: saw end prompt, pushing stream_done (chunks pushed={response_chunk_count[0]})", context=self._log_prefix)
                self._current_queue.put(("stream_done", None))
                self._current_queue = None
                self._response_done.set()
                self._reader_ready.set()
                response_chunk_count[0] = 0
            else:
                self._reader_ready.set()
                if line_count[0] <= 50 or line_count[0] % 100 == 0:
                    debug_log(f"reader_loop: saw prompt (confirming readiness)", context=self._log_prefix)
            return

        if not self.should_forward_chunk(line):
            return

        response_chunk_count[0] += 1
        if response_chunk_count[0] <= 3 or response_chunk_count[0] % 100 == 0:
            debug_log(f"reader_loop: response chunk #{response_chunk_count[0]} {preview}", context=self._log_prefix)
        self._current_queue.put(("chunk", line if line.endswith("\n") else line + "\n"))

    def _reader_loop(self, stdout_stream):
        line_count = [0]
        response_chunk_count = [0]
        debug_log("reader_loop: started", context=self._log_prefix)

        buf = ""
        try:
            fd = stdout_stream.fileno() if hasattr(stdout_stream, "fileno") else None

            while not self._stop_requested:
                if fd is None:
                    chunk = stdout_stream.read(1)
                else:
                    try:
                        b = os.read(fd, 1024)
                        chunk = b.decode("utf-8", errors="replace")
                    except BlockingIOError:
                        time.sleep(0.01)
                        continue
                    except Exception:
                        chunk = stdout_stream.read(1)

                if not chunk:
                    if buf:
                        line_count[0] += 1
                        self._process_line(buf, line_count, response_chunk_count)
                    break

                buf += chunk

                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line_count[0] += 1
                    self._process_line(line + "\n", line_count, response_chunk_count)

                if buf and self.is_ready_prompt(buf) and self._current_queue is None:
                    line_count[0] += 1
                    self._process_line(buf, line_count, response_chunk_count)
                    buf = ""
                elif buf and self.is_end_of_response(buf) and self._current_queue is not None:
                    line_count[0] += 1
                    self._process_line(buf, line_count, response_chunk_count)
                    buf = ""

        except (OSError, IOError) as e:
            if getattr(e, "errno", None) == 5:
                proc = None
                try:
                    with self._lock:
                        proc = self._process
                except Exception:
                    pass
                alive = proc is not None and proc.poll() is None
                stderr_snippet = ("; ".join(self._stderr_lines[-5:])) if self._stderr_lines else ""
                debug_log(f"reader_loop: EIO (errno 5) - process_alive={alive} returncode={getattr(proc, 'returncode', None) if proc else None}; stderr tail: {stderr_snippet[:200]}", context=self._log_prefix)
                if self._current_queue is not None:
                    msg = (
                        f"{self.display_name} subprocess ended unexpectedly (I/O error). "
                        "Check backend configuration."
                    )
                    self._current_queue.put(("error", RuntimeError(msg)))
            else:
                debug_log(f"reader_loop: exception {e}", context=self._log_prefix)
                if self._current_queue is not None:
                    self._current_queue.put(("error", e))
        except Exception as e:
            debug_log(f"reader_loop: exception {e}", context=self._log_prefix)
            if self._current_queue is not None:
                self._current_queue.put(("error", e))
        finally:
            debug_log(f"reader_loop: exiting (total lines read={line_count[0]}), setting _response_done", context=self._log_prefix)
            self._response_done.set()
            self._current_queue = None

    def _ensure_process(self, path, args_str, queue, stop_checker, cwd=None, env=None):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                debug_log(f"ensure_process: reusing existing process (pid={getattr(self._process, 'pid', None)})", context=self._log_prefix)
                return self._process, True

            debug_log("ensure_process: no live process, starting new one", context=self._log_prefix)
            self._reader_ready.clear()
            self._current_queue = None
            self._response_done.clear()
            self._stderr_lines[:] = []
            if self._pty_master_write is not None:
                try:
                    self._pty_master_write.close()
                except Exception:
                    pass
                self._pty_master_write = None

            if path:
                base_cmd = [path]
            else:
                base_cmd = shlex.split(self.get_default_cmd())

            if args_str:
                base_cmd.extend(shlex.split(args_str))

            use_pty = _PTY_AVAILABLE and os.name != "nt"
            stdout_stream = None
            if use_pty:
                master_read = None
                try:
                    master_fd, slave_fd = pty.openpty()
                    if _WINSIZE_AVAILABLE:
                        try:
                            # Set terminal size and disable echo
                            attr = termios.tcgetattr(slave_fd)
                            attr[3] = attr[3] & ~termios.ECHO
                            termios.tcsetattr(slave_fd, termios.TCSANOW, attr)
                            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                        except Exception as e:
                            debug_log(f"ensure_process: PTY config failed {e} (continuing)", context=self._log_prefix)

                    master_read_fd = os.dup(master_fd)
                    master_read = open(master_read_fd, "r", encoding="utf-8", errors="replace", newline="\n")
                    self._pty_master_write = open(master_fd, "w", encoding="utf-8", errors="replace", newline="\n")

                    try:
                        self._process = subprocess.Popen(
                            base_cmd,
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=subprocess.PIPE,
                            env=env if env else os.environ.copy(),
                            cwd=cwd,
                            start_new_session=True,
                        )
                        os.close(slave_fd)
                        slave_fd = None
                    except Exception:
                        if slave_fd is not None:
                            os.close(slave_fd)
                        raise

                    stdout_stream = master_read
                    debug_log(f"ensure_process: Popen with PTY ok (echo disabled), pid={self._process.pid}", context=self._log_prefix)
                except Exception as e:
                    debug_log(f"ensure_process: PTY spawn failed {e}, falling back to pipes", context=self._log_prefix)
                    if master_read is not None:
                        try:
                            master_read.close()
                        except Exception:
                            pass
                    if self._pty_master_write is not None:
                        try:
                            self._pty_master_write.close()
                        except Exception:
                            pass
                        self._pty_master_write = None
                    use_pty = False

            if not use_pty:
                cmd = base_cmd
                if shutil.which("stdbuf"):
                    cmd = ["stdbuf", "-o", "L"] + base_cmd
                debug_log(f"ensure_process: using pipes, cmd={cmd}", context=self._log_prefix)
                try:
                    self._process = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env if env else os.environ.copy(),
                        cwd=cwd,
                        bufsize=1,
                        start_new_session=True,
                    )
                except FileNotFoundError as e:
                    debug_log(f"ensure_process: FileNotFoundError {e}", context=self._log_prefix)
                    return None, False
                except Exception as e:
                    debug_log(f"ensure_process: Popen failed {e}", context=self._log_prefix)
                    return None, False
                stdout_stream = self._process.stdout

            try:
                from plugin.framework.worker_pool import run_in_background
                self._reader_thread = run_in_background(
                    self._reader_loop, stdout_stream, name=f"{self.display_name}-reader"
                )
                _stderr_thread = run_in_background(
                    self._stderr_drain_loop, self._process, name=f"{self.display_name}-stderr"
                )
            except Exception as e:
                debug_log(f"ensure_process: failed to start reader/stderr {e}", context=self._log_prefix)
                if self._process:
                    try:
                        self._process.terminate()
                    except Exception:
                            pass
                self._process = None
                return None, False

            debug_log("ensure_process: process and reader started", context=self._log_prefix)
        return self._process, True

    def stop(self):
        self._stop_requested = True
        with self._lock:
            proc = self._process
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
        self._response_done.set()

    def _get_agent_env(self, ctx):
        env = os.environ.copy()
        try:
            from plugin.framework.config import get_api_config
            api_cfg = get_api_config(ctx)
            if api_cfg.apiKey:
                # Common aider/proxy env vars
                env["OPENAI_API_KEY"] = api_cfg.apiKey
                env["OPENROUTER_API_KEY"] = api_cfg.apiKey
                env["ANTHROPIC_API_KEY"] = api_cfg.apiKey
                env["AIDER_OPENROUTER_API_KEY"] = api_cfg.apiKey
            if api_cfg.model:
                env["AIDER_MODEL"] = api_cfg.model
            if api_cfg.endpoint:
                env["OPENAI_API_BASE"] = api_cfg.endpoint
        except Exception:
            pass
        return env

    def _get_agent_cwd(self, document_url):
        if not document_url:
            return None
            
        try:
            if document_url.startswith("file://"):
                path = document_url[7:]
                # Handle encoded URLs if necessary, but UNO usually gives us straight paths
                if os.path.isfile(path):
                    return os.path.dirname(path)
                elif os.path.isdir(path):
                    return path
        except Exception:
            pass
        return None

    def send(
        self,
        queue,
        user_message,
        document_context,
        document_url,
        system_prompt=None,
        mcp_url=None,
        selection_text=None,
        stop_checker=None,
        **kwargs
    ):
        self._stop_requested = False

        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "agent_backend.path") or "").strip()
            args_str = str(get_config(self._ctx, "agent_backend.args") or "").strip()
        except Exception:
            path = ""
            args_str = ""

        stdin_payload = self.format_input(
            user_message, document_context, document_url, system_prompt, selection_text, mcp_url=mcp_url, **kwargs
        )
        
        cwd = self._get_agent_cwd(document_url)
        if not cwd:
            cwd = os.path.expanduser("~")
        env = self._get_agent_env(self._ctx)

        with self._lock:
            need_start = self._process is None or (self._process and self._process.poll() is not None)

        debug_log(f"send(): entry, path={path or self.get_default_cmd()}, need_start={need_start}, cwd={cwd}", context=self._log_prefix)
        queue.put(("status", f"Starting {self.display_name}..." if need_start else "Sending..."))

        proc, ok = self._ensure_process(path, args_str, queue, stop_checker, cwd=cwd, env=env)
        if not ok:
            debug_log(f"send(): _ensure_process returned not ok, proc={proc}", context=self._log_prefix)
            if proc is None:
                queue.put((
                    "error",
                    RuntimeError(
                        f"{self.display_name} not found. Install it or set Settings → Agent backends → Path."
                    ),
                ))
            else:
                queue.put(("error", RuntimeError(f"{self.display_name} did not start correctly within 30s.")))
            return

        queue.put(("status", f"Waiting for {self.display_name}..."))
        debug_log(f"send(): checking if {self.display_name} is ready...", context=self._log_prefix)
        # Always wait for the initial or previous turn's prompt
        if not self._reader_ready.wait(timeout=30.0):
            debug_log(f"send(): timed out waiting for {self.display_name} ready prompt", context=self._log_prefix)
            # Check if it died while we were waiting
            if proc.poll() is not None:
                debug_log(f"send(): {self.display_name} died during wait, code={proc.returncode}", context=self._log_prefix)
                # Fall through to the error handler logic at the end of send() or handle here
                self._current_queue = None
                queue.put(("error", RuntimeError(f"{self.display_name} exited with code {proc.returncode} before turn started.")))
                return
            # We'll try to proceed anyway, but it might fail
        else:
            debug_log(f"send(): {self.display_name} is ready", context=self._log_prefix)

        queue.put(("status", f"Sending to {self.display_name}..."))
        debug_log(f"send(): process ready, pid={getattr(proc, 'pid', None)}, writing payload ({len(stdin_payload)} bytes)", context=self._log_prefix)
        self._response_done.clear()
        self._reader_ready.clear()
        self._current_queue = queue

        try:
            stdin_stream = self._pty_master_write if self._pty_master_write is not None else proc.stdin
            stdin_stream.write(stdin_payload)
            stdin_stream.flush()
            debug_log("send(): payload written, waiting for _response_done", context=self._log_prefix)
        except Exception as e:
            self._current_queue = None
            debug_log(f"send(): write failed {e}", context=self._log_prefix)
            queue.put(("error", e))
            return

        timeout_seconds = 300
        deadline = time.monotonic() + timeout_seconds
        last_log = [time.monotonic()]

        while not self._response_done.is_set() and time.monotonic() < deadline:
            if self._stop_requested or (stop_checker and stop_checker()):
                debug_log("send(): stop requested while waiting", context=self._log_prefix)
                break
            now = time.monotonic()
            elapsed = now - (deadline - timeout_seconds)
            if now - last_log[0] >= 5.0:
                debug_log(f"send(): still waiting for _response_done, proc.alive={proc.poll() is None}, elapsed={elapsed:.1f}s", context=self._log_prefix)
                last_log[0] = now
            self._response_done.wait(timeout=0.25)

        self._current_queue = None
        elapsed = time.monotonic() - (deadline - timeout_seconds)
        debug_log(f"send(): done waiting, stopped={self._stop_requested} returncode={getattr(proc, 'returncode', None)} elapsed={elapsed:.1f}s", context=self._log_prefix)

        if proc.poll() is not None and proc.returncode != 0:
            try:
                err = proc.stderr.read() if proc.stderr else ""
                err = strip_ansi(err).strip()
            except Exception:
                err = ""
            if not err and self._stderr_lines:
                err = "; ".join(self._stderr_lines[-8:])
            if not err:
                err = f"{self.display_name} exited with code {proc.returncode}."
            debug_log(f"send(): process exited, returncode={proc.returncode}, stderr: {err[:300]}", context=self._log_prefix)
            queue.put(("error", RuntimeError(err)))
        elif self._stop_requested or (stop_checker and stop_checker()):
            queue.put(("stopped",))
        else:
            queue.put(("stream_done", None))
