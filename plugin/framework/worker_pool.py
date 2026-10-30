# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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

"""Centralized management for background worker threads and external subprocesses.

Lock rules:
- ``_pool_lock``: Guards creating the global pool instance.
- ``StderrTail._lock``: Protects only the bounded child stderr chunk deque and character count; synchronizes background drain appends with consumer reads.
- No pooled joins from pool workers: Enforced via thread-local flag in ``BackgroundHandle.join``; prevents deadlocking the fixed-size pool when all workers block on queued tasks.
- Dedicated threads for blocking jobs: Servers, long pipes, and join targets pass ``dedicated=True``; prevents permanently exhausting fixed pool worker slots.
"""

from __future__ import annotations

import codecs
import contextvars
import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections import deque
from concurrent.futures import CancelledError, Future, TimeoutError as FuturesTimeoutError
from typing import Optional, Callable, Any, ClassVar, IO, Iterator

from plugin.framework.constants import BACKGROUND_POOL_MAX_WORKERS
from plugin.framework.errors import ToolExecutionError, WorkerPoolError

# Thread-safety guard (Layer A): tag threads born here so assert_main_thread
# can name the offending background task in diagnostics.
from plugin.framework import thread_guard

log = logging.getLogger("writeragent.framework.worker_pool")

_DEFAULT_STDERR_TAIL_CHARS = 8192
_MAX_STREAM_LINE_CHARS = 1024 * 1024

_pool_lock = threading.Lock()
_pool_worker_local = threading.local()


def _in_pool_worker() -> bool:
    return getattr(_pool_worker_local, "active", False)


_pool: "_DaemonWorkPool | None" = None


def background_pool_max_workers() -> int:
    """Resolved pool size: env, then ``BACKGROUND_POOL_MAX_WORKERS``."""
    raw = os.environ.get("WRITERAGENT_BG_POOL_WORKERS")
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return BACKGROUND_POOL_MAX_WORKERS


def _future_stored_this_exception(fut: Future[Any], exc: BaseException) -> bool:
    if not fut.done():
        return False
    if fut.cancelled():
        # result() raises a new CancelledError. exception() raises too, so identity cannot see a cancel.
        return isinstance(exc, CancelledError)
    return fut.exception() is exc


class BackgroundHandle:
    """Joinable handle for pooled or dedicated ``run_in_background`` work.

    Matches the ``Thread.join`` / ``Thread.is_alive`` surface callers already use.
    ``join`` does not re-raise exceptions raised by the worker, including
    ``SystemExit`` and ``KeyboardInterrupt`` stored on the pooled future (the
    pool already logged those). ``KeyboardInterrupt`` raised on the joining
    thread while ``join`` is blocked still propagates.
    """

    __slots__: ClassVar[tuple[str, ...]] = ("_future", "_thread")
    _future: Future[Any] | None
    _thread: threading.Thread | None

    def __init__(self, *, future: Future[Any] | None = None, thread: threading.Thread | None = None) -> None:
        self._future = future
        self._thread = thread

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return
        fut = self._future
        if fut is None:
            return
        if fut.done():
            return
        # The pool-worker flag is set only in _run and is not a contextvar, so
        # caller_ctx.run cannot clear it and a dedicated thread never sets it.
        # A finished future needs no pool worker; checking fut.done() first avoids a
        # false deadlock.
        if _in_pool_worker():
            raise RuntimeError("join() of a pooled job from a pool thread would deadlock the background pool")
        try:
            fut.result(timeout=timeout)
        except FuturesTimeoutError:
            return
        except BaseException as exc:
            # Swallowing the worker's stored exception returns when the raised object
            # is the one stored on the future, and re-raises when it is not (e.g.
            # KeyboardInterrupt raised on the joining thread).
            if _future_stored_this_exception(fut, exc):
                return
            raise

    def is_alive(self) -> bool:
        thread = self._thread
        if thread is not None:
            return thread.is_alive()
        fut = self._future
        return fut is not None and not fut.done()

    def is_current_thread(self) -> bool:
        """True when this handle is the dedicated thread now running.

        One supported check. Callers that read ``_thread`` to skip a
        self-join miss a pooled handle: it has no thread, so that check is
        false and ``join`` runs. From a pool worker that raises,
        which is the deadlock guard and must stay. Pool workers that join
        a different pooled future still hit it.
        """
        thread = self._thread
        return thread is not None and thread is threading.current_thread()


class _DaemonWorkPool:
    """Fixed daemon workers + unbounded queue. ThreadPoolExecutor is non-daemon on 3.9+.

    SimpleQueue has no maxsize on purpose: a bounded queue would block or drop
    fire-and-forget UI work. Bound the *worker count*, not the submit queue.
    """

    _max_workers: int

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max(1, max_workers)
        self._queue: queue.SimpleQueue[tuple[Callable[[], None], Future[Any]]] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        for i in range(self._max_workers):
            t = threading.Thread(target=self._run, name=f"wa-bg-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Callable[[], None]) -> Future[Any]:
        fut: Future[Any] = Future()
        self._queue.put((fn, fut))
        return fut

    def _run(self) -> None:
        # The flag has to be set before caller_ctx.run, and it has to be a
        # thread-local so the copied context does not drop it while the job is inside join.
        _pool_worker_local.active = True
        while True:
            fn, fut = self._queue.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fn()
            except BaseException as exc:
                # Log BaseException before setting on the Future so unexpected exits are visible.
                log.exception("Unhandled %s in pooled background job", type(exc).__name__)
                # Pool workers must complete the Future even on SystemExit/KeyboardInterrupt.
                # Dedicated run_in_background threads catch Exception only (those must unwind).
                fut.set_exception(exc)
            else:
                fut.set_result(None)


def _get_pool() -> _DaemonWorkPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DaemonWorkPool(background_pool_max_workers())
        return _pool


def run_in_background(func: Callable[..., Any], *args: Any, name: str | None = None, error_callback: Callable[[WorkerPoolError], None] | None = None, daemon: bool = True, dedicated: bool = False, **kwargs: Any) -> BackgroundHandle:
    """Run *func* off the caller thread with WorkerPoolError isolation and Layer A tagging.

    Short fire-and-forget work is queued on a daemon pool with a fixed worker
    count (unbounded submit queue). Pass ``dedicated=True`` (or ``daemon=False``)
    for servers, pipe drains, infinite loops, and any job another thread will
    ``join()`` — those must not occupy a pool slot.

    Without *error_callback*, failures are logged only; ``BackgroundHandle.join()``
    does not re-raise exceptions raised by the worker, including ``SystemExit``
    and ``KeyboardInterrupt`` stored on the pooled future (the pool already logged
    those). ``KeyboardInterrupt`` raised on the joining thread while ``join`` is
    blocked still propagates.

    *name* is the Layer A task tag for every job; a dedicated thread also uses
    it as ``threading.Thread.name``, while a pooled job keeps ``wa-bg-N``. The
    pool-deadlock ``RuntimeError`` is ``_in_pool_worker()``. A dedicated
    thread keeps that flag false when its name starts with ``wa-bg-``.

    :return: A :class:`BackgroundHandle` with ``join`` / ``is_alive``.
    """

    # Stop cancellation is a contextvar on the send thread; copy caller context
    # so enqueued items inherit it and respect cancel_pending_work.
    caller_ctx = contextvars.copy_context()

    def _worker() -> None:
        task_id = str(uuid.uuid4())
        task_name = name or getattr(func, "__name__", "anon")
        log.debug("Starting task %s: %s", task_id, task_name)

        # Tag this background thread for the UNO thread-safety guard (Layer A).
        # This lets violations report the specific worker (e.g. "run_search") instead of a generic thread name.
        thread_guard.set_background_task(task_name)

        try:
            func(*args, **kwargs)
            log.debug("Task %s completed successfully", task_id)
        except Exception as e:
            # Not BaseException: SystemExit/KeyboardInterrupt must still unwind on
            # dedicated threads. Pool _run catches BaseException to finish the Future.
            error_id = str(uuid.uuid4())
            log.exception("Task %s failed", task_id, extra={"task_id": task_id, "task_name": task_name, "error_id": error_id, "error_type": type(e).__name__})

            wrapped_error = WorkerPoolError(f"Task '{task_name}' failed", code="WORKER_TASK_FAILED", details={"task_id": task_id, "task_name": task_name, "error_id": error_id, "original_error": str(e), "error_type": type(e).__name__})

            if error_callback:
                try:
                    error_callback(wrapped_error)
                except Exception:
                    log.exception("Error in error_callback for '%s'", task_name)
        finally:
            thread_guard.set_background_task(None)

    def _run_in_caller_context() -> None:
        caller_ctx.run(_worker)

    use_dedicated = dedicated or (daemon is False)
    if use_dedicated:
        thread_name = name or f"worker-{getattr(func, '__name__', 'anon')}"
        t = threading.Thread(target=_run_in_caller_context, name=thread_name, daemon=daemon)
        t.start()
        return BackgroundHandle(thread=t)

    fut = _get_pool().submit(_run_in_caller_context)
    return BackgroundHandle(future=fut)


def get_subprocess_creationflags() -> dict[str, Any]:
    """Return popen/run kwargs to hide command prompt windows on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


class StderrTail:
    """Bounded stderr text captured by a continuous drain thread.

    Prevents the classic OS pipe deadlock: child fills stderr while parent
    blocks on stdin/stdout. Keeps a diagnostic tail for crash messages.
    """

    __slots__: ClassVar[tuple[str, ...]] = ("_lock", "_chunks", "_chars", "_max_chars", "_thread")
    _lock: threading.Lock
    _chars: int
    _max_chars: int

    def __init__(self, max_chars: int = _DEFAULT_STDERR_TAIL_CHARS) -> None:
        self._lock = threading.Lock()
        self._chunks: deque[str] = deque()
        self._chars = 0
        self._max_chars = max(256, max_chars)
        self._thread: BackgroundHandle | threading.Thread | None = None

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._chars += len(text)
            while self._chunks and self._chars > self._max_chars:
                dropped = self._chunks.popleft()
                self._chars -= len(dropped)

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def finish_text(self, timeout: float = 1.0) -> str:
        """Join the drain after the child pipe should be at EOF, then return the tail.

        *timeout* is bounded so a stderr pipe that is still open cannot block
        the caller. Live readers (the child is still running) should use
        :meth:`text` instead.
        """
        self.join(timeout)
        return self.text()

    def attach_thread(self, thread: BackgroundHandle | threading.Thread) -> None:
        self._thread = thread

    def join(self, timeout: float | None = None) -> None:
        """Wait for the drain thread after its child pipe reaches EOF."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        """Return whether the drain thread is still consuming the pipe."""
        thread = self._thread
        return thread is not None and thread.is_alive()


def _read_stderr_chunk(stream: IO[Any]) -> bytes | str | None:
    """One short read. Empty means EOF. None means the pipe is already gone.

    ``BufferedReader.read(n)`` keeps reading until *n* bytes arrive or the
    pipe hits EOF, so a short burst from a still-running child never reached
    the tail. ``read1`` returns the bytes already in the pipe. A raw
    ``FileIO`` (``bufsize=0``) has no ``read1``; its ``read(n)`` is one
    ``os.read`` and already returns a short count.
    """
    buffer = getattr(stream, "buffer", None)
    readable: IO[Any] = buffer if buffer is not None and hasattr(buffer, "read1") else stream
    read1 = getattr(readable, "read1", None)
    # What was wrong: read1 had an odd `isinstance(chunk, (bytes, str)) or chunk is None` check
    # that redundantly returned None either way when not bytes/str, while read lacked narrowing.
    # How it happened: defensive check against unconfigured mock objects in tests.
    # Why this change fixes it: reads read1 or read and returns chunk only when bytes/str, else None.
    try:
        if callable(read1):
            chunk = read1(4096)
        else:
            chunk = readable.read(4096)
        if isinstance(chunk, (bytes, str)):
            return chunk
        return None
    except (OSError, ValueError):
        # ValueError: I/O operation on closed file, same as AsyncProcess readers.
        return None


def _iter_decoded_chunks(stream: IO[Any]) -> Iterator[str]:
    """Yield text from ``_read_stderr_chunk``, joining UTF-8 split across reads.

    One decoder feeds ``_read_stream``, ``_drain_stream``, and
    ``start_stderr_drain``. Decoding each chunk with ``errors='replace'``
    turns a code point split across two short reads into U+FFFD twice.
    ``final=True`` runs only at EOF, so a trailing incomplete sequence is
    replaced once, not per read.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = _read_stderr_chunk(stream)
        if chunk is None or chunk == "" or chunk == b"":
            break
        if isinstance(chunk, str):
            text = chunk
        else:
            text = decoder.decode(chunk)
        if text:
            yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def _split_on_newlines(pending: str) -> tuple[list[str], str]:
    """Split on ``\\n``, ``\\r\\n``, and a lone ``\\r``. Hold a trailing ``\\r``.

    ``str.splitlines`` also breaks on vertical tab, form feed, and the
    Unicode line separators. The callback then sees a new line with that
    separator still in the text, because ``rstrip`` only removes ``\\n``
    and ``\\r``. A trailing ``\\r`` stays in the remainder so the next
    chunk can finish a CRLF instead of emitting a line early.
    """
    lines: list[str] = []
    start = 0
    index = 0
    limit = len(pending)
    while index < limit:
        char = pending[index]
        if char == "\n":
            lines.append(pending[start : index + 1])
            index += 1
            start = index
            continue
        if char == "\r":
            if index + 1 == limit:
                break
            end = index + 2 if pending[index + 1] == "\n" else index + 1
            lines.append(pending[start:end])
            index = end
            start = index
            continue
        index += 1
    return lines, pending[start:]


def _feed_line_buffer(parts: list[str], length: int, chunk: str) -> tuple[list[str], list[str], int]:
    """Feed one new chunk into line-buffered pieces without rescanning the remainder.

    Bugfix: The remainder has already been scanned and has no complete line,
    so scanning it again on every 4 KB read was O(n²), and a newline-free child
    kept every byte. Pieces stay unjoined until a line is emitted or the cap is hit.
    """
    if not chunk:
        return [], parts, length

    if parts and parts[-1].endswith("\r"):
        last = parts[-1]
        if last == "\r":
            parts.pop()
        else:
            parts[-1] = last[:-1]
        length -= 1
        chunk = "\r" + chunk

    lines, rest = _split_on_newlines(chunk)
    if lines:
        lines[0] = "".join(parts) + lines[0]
        if rest:
            return lines, [rest], len(rest)
        return lines, [], 0

    if rest:
        parts.append(rest)
        length += len(rest)
    return [], parts, length


def _split_overflow(parts: list[str], length: int, limit: int) -> tuple[list[str], list[str], int]:
    """Split unjoined pieces exceeding *limit* characters into partial line slices."""
    if length <= limit:
        return [], parts, length
    text = "".join(parts)
    slices: list[str] = []
    while len(text) > limit:
        slices.append(text[:limit])
        text = text[limit:]
    new_parts = [text] if text else []
    return slices, new_parts, len(text)


def start_stderr_drain(stream: IO[Any] | None, *, max_tail_chars: int = _DEFAULT_STDERR_TAIL_CHARS, name: str = "stderr-drain") -> StderrTail | None:
    """Continuously drain a child stderr pipe into a bounded :class:`StderrTail`.

    Call this immediately after ``Popen(..., stderr=PIPE)`` for long-lived workers.
    Returns None when *stream* is None (e.g. stderr redirected to DEVNULL).
    """
    if stream is None:
        return None
    tail = StderrTail(max_chars=max_tail_chars)

    def _loop() -> None:
        try:
            for text in _iter_decoded_chunks(stream):
                tail._append(text)
        except Exception:
            log.debug("%s failed", name, exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:
                log.debug("%s close failed", name, exc_info=True)

    thread = run_in_background(_loop, name=name, dedicated=True)
    tail.attach_thread(thread)
    return tail


def _quiet_close(stream: Any) -> None:
    """Close one pipe. OSError means the fd is already gone. Do not catch ValueError."""
    try:
        stream.close()
    except OSError:
        pass


def _kill_child(proc: subprocess.Popen[Any] | None, managed: Any) -> None:
    """SIGKILL via the managed tree when present, otherwise the bare process.

    managed.kill_tree() swallows Exception and logs. The bare kill swallows
    only OSError. Leave unregister() at the call site: kill_tree() must not
    unregister, and _reap unregisters after the wait, not inside the kill.
    """
    if managed is not None:
        managed.kill_tree()
        return
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass


class AsyncProcess:
    """Run a child process and deliver stdout, stderr, and exit on dedicated threads.

    The caller serializes ``start`` and ``terminate``. The MCP tunnel holds
    ``_lock`` across both. This class does not take its own lock: a line
    callback already runs under the caller's lock and calls ``terminate``,
    and ``terminate`` returns before the reaper joins those readers.
    ``terminate`` closes pipes on the ``Popen`` it captured before
    ``Popen.terminate`` (that syscall releases the GIL). ``_wait_for_exit``
    and ``_reap`` already receive that same captured child.
    """

    args: str | list[str]
    stdout_cb: Optional[Callable[[str], None]]
    stderr_cb: Optional[Callable[[str], None]]
    on_exit_cb: Optional[Callable[[int], None]]

    def __init__(self, args: str | list[str], stdout_cb: Optional[Callable[[str], None]] = None, stderr_cb: Optional[Callable[[str], None]] = None, on_exit_cb: Optional[Callable[[int], None]] = None, **popen_kwargs: Any) -> None:
        self.args = args
        self.stdout_cb = stdout_cb
        self.stderr_cb = stderr_cb
        self.on_exit_cb = on_exit_cb
        self._child_generation: int = 0
        # Pipes stay binary. text=True is ignored; see the popen kwargs below.
        self.process: Optional[subprocess.Popen[Any]] = None

        # Copy: setdefault must not mutate the caller's dict.
        self._popen_kwargs: dict[str, Any] = dict(popen_kwargs)
        for key, value in get_subprocess_creationflags().items():
            self._popen_kwargs.setdefault(key, value)
        self._popen_kwargs.setdefault("stdout", subprocess.PIPE)
        self._popen_kwargs.setdefault("stderr", subprocess.PIPE)
        if sys.platform != "win32":
            self._popen_kwargs.setdefault("start_new_session", True)
        # Force binary pipes. _read_stream reads the binary buffer and
        # decodes UTF-8; a TextIOWrapper plus that read desyncs the two
        # buffers. No AsyncProcess caller passes text=True (the tunnel uses
        # the binary default). encoding, errors, or universal_newlines
        # would still open text mode after text=False.
        if self._popen_kwargs.get("text") or self._popen_kwargs.get("universal_newlines") or self._popen_kwargs.get("encoding") or self._popen_kwargs.get("errors"):
            log.debug("AsyncProcess forces binary pipes; text mode and encoding arguments are ignored")
        self._popen_kwargs["text"] = False
        self._popen_kwargs.pop("encoding", None)
        self._popen_kwargs.pop("errors", None)
        self._popen_kwargs.pop("universal_newlines", None)
        self._popen_kwargs.setdefault("bufsize", 0)

        self._managed: Any = None
        self._stdout_thread: BackgroundHandle | None = None
        self._stderr_thread: BackgroundHandle | None = None
        self._wait_thread: BackgroundHandle | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        """Starts the process and its monitoring threads."""
        # The generation is the child's identity; only the latest one notifies.
        self._child_generation += 1
        generation = self._child_generation
        if self.process is not None and self.process.poll() is None:
            # terminate() returns before the child is dead so it cannot join
            # callback threads on this stack. Wait here, without those joins.
            old = self.process
            old_managed = self._managed
            self.terminate()
            try:
                old.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("Previous process still alive after terminate")
                if old_managed is not None:
                    old_managed.kill_tree()
        try:
            proc: subprocess.Popen[Any] = subprocess.Popen(self.args, **self._popen_kwargs)
        except Exception as e:
            raise ToolExecutionError(f"Failed to start process: {self.args}", details={"error": str(e)}) from e

        self.process = proc
        from plugin.framework.managed_subprocess import ManagedSubprocess

        managed = ManagedSubprocess.wrap_existing(proc, label=f"asyncproc-{getattr(proc, 'pid', 'unknown')}", register=True)
        self._managed = managed
        stdout_thread: BackgroundHandle | None = None
        stderr_thread: BackgroundHandle | None = None
        wait_thread: BackgroundHandle | None = None
        try:
            if proc.stdout and self.stdout_cb:
                stdout_thread = run_in_background(self._read_stream, proc.stdout, self.stdout_cb, name=f"asyncproc-out-{proc.pid}", dedicated=True)
            elif proc.stdout:
                # Drain it silently to avoid deadlocks
                stdout_thread = run_in_background(self._drain_stream, proc.stdout, name=f"asyncproc-outdrain-{proc.pid}", dedicated=True)

            if proc.stderr and self.stderr_cb:
                stderr_thread = run_in_background(self._read_stream, proc.stderr, self.stderr_cb, name=f"asyncproc-err-{proc.pid}", dedicated=True)
            elif proc.stderr:
                stderr_thread = run_in_background(self._drain_stream, proc.stderr, name=f"asyncproc-errdrain-{proc.pid}", dedicated=True)

            # Pass this child and these readers. _wait_for_exit must not read
            # self.process later: a second start() replaces them.
            wait_thread = run_in_background(self._wait_for_exit, proc, stdout_thread, stderr_thread, generation, managed, name=f"asyncproc-wait-{proc.pid}", dedicated=True)
        except BaseException:
            # On thread spawn failure, clear self.process and the handle fields,
            # kill the child process tree and wait with a 1.0s timeout directly on
            # this stack (since terminate()'s reaper would need another thread),
            # close unattached pipes via _close_child_pipes, and re-raise.
            self.process = None
            self._managed = None
            self._stdout_thread = None
            self._stderr_thread = None
            self._wait_thread = None
            _kill_child(proc, managed)
            if managed is not None:
                managed.unregister()
            try:
                proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._close_child_pipes(proc, stdout_thread=stdout_thread, stderr_thread=stderr_thread)
            raise

        self._stdout_thread = stdout_thread
        self._stderr_thread = stderr_thread
        self._wait_thread = wait_thread

    def _read_stream(self, stream: Any, callback: Any) -> None:
        # Feed only new text, hold pieces in a list until a newline is found, and
        # cap the unjoined buffer at _MAX_STREAM_LINE_CHARS to deliver partial lines
        # instead of leaking memory. Short reads go through ``_iter_decoded_chunks``
        # so a UTF-8 code point split across reads is not replaced twice. The callback
        # try/except stays inside the loop: a bad callback must not close the pipe early.
        def emit(line: str) -> None:
            try:
                callback(line.rstrip("\n\r"))
            except Exception:
                log.exception("AsyncProcess stream callback failed")

        parts: list[str] = []
        length = 0
        warned = False
        try:
            for chunk in _iter_decoded_chunks(stream):
                lines, parts, length = _feed_line_buffer(parts, length, chunk)
                for line in lines:
                    emit(line)
                while length > _MAX_STREAM_LINE_CHARS:
                    if not warned:
                        log.warning(
                            "AsyncProcess line exceeded %d characters without a newline; delivering a partial line",
                            _MAX_STREAM_LINE_CHARS,
                        )
                        warned = True
                    overflow_lines, parts, length = _split_overflow(parts, length, _MAX_STREAM_LINE_CHARS)
                    for slice_line in overflow_lines:
                        emit(slice_line)
        finally:
            if parts:
                emit("".join(parts))
            _quiet_close(stream)

    def _drain_stream(self, stream: Any) -> None:
        try:
            for _text in _iter_decoded_chunks(stream):
                pass
        finally:
            _quiet_close(stream)

    def _join_handles(self, handles: tuple[BackgroundHandle | None, ...], timeout: float | None = 1.0) -> None:
        """Join reader threads unless this stack is that thread.

        ``timeout`` is forwarded to ``join``. None would wait until the
        reader returns. ``_reap`` and ``_wait_for_exit`` keep a bound so a
        pipe a grandchild inherited cannot block forever. A reader that
        finishes within the bound still delivers its trailing line first.
        """
        for handle in handles:
            if handle is None:
                continue
            # Future-backed handles are not this thread. join() still
            # raises from a pool worker so the pool cannot deadlock.
            if handle.is_current_thread():
                continue
            handle.join(timeout=timeout)

    def _close_child_pipes(self, proc: subprocess.Popen[Any] | None, *, stdout_thread: BackgroundHandle | None = None, stderr_thread: BackgroundHandle | None = None) -> None:
        # Closes the Popen the caller passed, the same capture _wait_for_exit
        # and _reap use.
        if proc is None:
            return
        if proc.stdin is not None:
            _quiet_close(proc.stdin)
        if stdout_thread is None and proc.stdout is not None:
            _quiet_close(proc.stdout)
        if stderr_thread is None and proc.stderr is not None:
            _quiet_close(proc.stderr)

    def _wait_for_exit(self, proc: subprocess.Popen[Any], stdout_thread: BackgroundHandle | None, stderr_thread: BackgroundHandle | None, generation: int, managed: Any = None) -> None:
        # Wait and join only this call's child. Reading self.process and
        # the reader handles at wait time follows start() if it runs again
        # and points those at a new child, so the first wait thread blocks
        # on that child and fires on_exit_cb for its exit as well. _reap
        # already captures the handles it was given.
        rc = proc.wait()
        if managed is not None:
            managed.unregister()
        # A bounded join still delivers a trailing line without a newline
        # when the readers finish in time, and on_exit_cb still runs when
        # they do not. timeout=None never returns if a grandchild inherited
        # stdout or stderr and never closes the pipe: the readers never see
        # EOF, on_exit_cb never runs, and this thread leaks. _reap uses the
        # same 1s bound.
        self._join_handles((stdout_thread, stderr_thread), timeout=1.0)
        args = self.args
        if isinstance(args, str):
            preview = args
        elif args:
            preview = args[0]
        else:
            preview = ""
        log.debug("Process %s exited with rc=%s", preview, rc)
        if generation != self._child_generation:
            log.debug("Suppressing on_exit_cb for superseded child rc=%s", rc)
            return
        if self.on_exit_cb:
            try:
                self.on_exit_cb(rc)
            except Exception:
                log.exception("Error in on_exit_cb for process")

    def _reap(self, proc: subprocess.Popen[Any], stdout_thread: BackgroundHandle | None, stderr_thread: BackgroundHandle | None, wait_thread: BackgroundHandle | None, timeout: float, managed: Any = None) -> None:
        """Wait, then SIGKILL via kill_tree, off the caller stack. See terminate().

        Handles are captured at terminate() time. A later start() replaces
        self.process; this reaper must not wait on that new child.
        """
        if proc.poll() is None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_child(proc, managed)
                # A stuck SIGKILL (uninterruptible sleep) must not block forever.
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    log.warning("Process still alive after kill (timeout=%ss)", timeout)
        if managed is not None:
            managed.unregister()
        self._join_handles((stdout_thread, stderr_thread, wait_thread))

    def terminate(self, timeout: float = 5.0) -> None:
        """Signal the child and return. Reap on a dedicated thread.

        Signal the child, close stdin (and close stdout/stderr only if that
        stream has no reader thread), and reap after the caller can drop its
        lock. Reader threads close their own pipe in their finally block once
        they see EOF. On Linux, close of the read fd from this thread does not
        interrupt a reader blocked in os.read (bufsize=0). The child's exit
        produces the EOF that wakes the read. Closing the read end first
        discards unread bytes and makes the child's shutdown write fail with
        EPIPE. Waiting up to 5s twice, then joining the stdout, stderr, and
        wait threads, freezes LibreOffice: MCP tunnel stop calls this while
        holding the lock those line callbacks need, including from the UI
        thread. A callback that called terminate joined itself. The timed
        wait stays on the reaper so a stuck SIGKILL cannot block forever.
        """
        proc = self.process
        if proc is None:
            return
        managed = self._managed
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread
        wait_thread = self._wait_thread
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.debug("terminate: process already gone", exc_info=True)
        self._close_child_pipes(proc, stdout_thread=stdout_thread, stderr_thread=stderr_thread)
        run_in_background(self._reap, proc, stdout_thread, stderr_thread, wait_thread, timeout, managed, name="asyncproc-reap", dedicated=True)

    def kill_tree(self) -> None:
        """Immediate SIGKILL of the process group (including grandchildren and helper processes)."""
        _kill_child(self.process, self._managed)
