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

Background threads created here are tagged (via thread_guard) so that the
UNO main-thread runtime guard (Layer A) can name the offending task on violation.

Concurrency: all background Python work must start here
(``run_in_background``), not via raw ``threading.Thread``. Short
fire-and-forget jobs share a **fixed-size daemon pool** (unbounded queue).
Servers, pipe drains, LLM streams, and anything another thread will
``join()`` must pass ``dedicated=True`` so they do not occupy a pool slot
forever. ``_pool_lock`` only covers creating the pool or swapping it
out. ``reset_background_pool_for_tests`` joins the old pool after
releasing that lock, so a job can call ``run_in_background`` during
reset. ``submit`` and the shutdown drain share a lock so a test reset cannot
enqueue work behind the worker sentinels. ``StderrTail``’s lock is only
the bounded stderr buffer from a child process. Never ``join()`` a pooled
job from another pooled job (the pool can deadlock). Map:
docs/framework/threading.md.
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
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from typing import Optional, Callable, Any, ClassVar, IO, Iterator

from plugin.framework.constants import BACKGROUND_POOL_MAX_WORKERS
from plugin.framework.errors import WorkerPoolError

log = logging.getLogger("writeragent.framework.worker_pool")

_DEFAULT_STDERR_TAIL_CHARS = 8192

# Thread-safety guard (Layer A): tag threads born here so assert_main_thread
# can name the offending background task in diagnostics.
from plugin.framework import thread_guard

_pool_lock = threading.Lock()
_pool: "_DaemonWorkPool | None" = None
_pool_size_override: int | None = None
# shutdown() joins in slices so a KeyboardInterrupt can land between them.
# One slice used to return while the in-flight job was still running.
_POOL_SHUTDOWN_JOIN_SLICE_SEC = 5.0


def background_pool_max_workers() -> int:
    """Resolved pool size: test override, then env, then ``BACKGROUND_POOL_MAX_WORKERS``."""
    if _pool_size_override is not None:
        return _pool_size_override
    raw = os.environ.get("WRITERAGENT_BG_POOL_WORKERS")
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return BACKGROUND_POOL_MAX_WORKERS


class BackgroundHandle:
    """Joinable handle for pooled or dedicated ``run_in_background`` work.

    Matches the ``Thread.join`` / ``Thread.is_alive`` surface callers already use.
    ``join`` never re-raises worker exceptions (those are logged / ``error_callback``).
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
        # join() from a wa-bg-* pool thread waits on a Future that only
        # another pool worker can finish. With two workers both blocked in
        # join, the pool deadlocks and the timeout hides it. A finished
        # future needs no pool worker; checking fut.done() first avoids a
        # false deadlock.
        if threading.current_thread().name.startswith("wa-bg-"):
            raise RuntimeError("join() of a pooled job from a pool thread would deadlock the background pool")
        try:
            fut.result(timeout=timeout)
        except FuturesTimeoutError:
            return
        except Exception:
            # concurrent.futures.CancelledError subclasses Exception (via Error),
            # including on Python 3.13. A cancelled future is swallowed here.
            # Do not catch BaseException: KeyboardInterrupt/SystemExit from
            # fut.set_exception must still escape join().
            return

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
        false and ``join`` runs. From a ``wa-bg-*`` worker that raises,
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
    _shutdown: bool
    _submit_lock: threading.Lock

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max(1, max_workers)
        self._queue: queue.SimpleQueue[tuple[Callable[[], None], Future[Any]] | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False
        # Held across the shutdown flag, the pending-work drain, and the
        # sentinel puts. submit() takes the same lock so a Future cannot land
        # behind those sentinels after the workers have been told to exit.
        self._submit_lock = threading.Lock()
        for i in range(self._max_workers):
            t = threading.Thread(target=self._run, name=f"wa-bg-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Callable[[], None]) -> Future[Any]:
        with self._submit_lock:
            if self._shutdown:
                raise RuntimeError("background pool is shut down")
            fut: Future[Any] = Future()
            self._queue.put((fn, fut))
            return fut

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fn()
            except BaseException as exc:
                # Pool workers must complete the Future even on SystemExit/KeyboardInterrupt.
                # Dedicated run_in_background threads catch Exception only (those must unwind).
                fut.set_exception(exc)
            else:
                fut.set_result(None)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        with self._submit_lock:
            self._shutdown = True
            # Drop the live pool prefix so tests (and diagnostics) do not count
            # retiring workers as the new pool. Happened when a prior 8-worker
            # pool's join timed out and a leftover ``wa-bg-3`` sat next to a
            # 2-worker reset pool's ``wa-bg-0`` / ``wa-bg-1``.
            for i, t in enumerate(self._threads):
                t.name = f"wa-bg-retired-{i}"
            if cancel_futures:
                pending: list[tuple[Callable[[], None], Future[Any]]] = []
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        continue
                    pending.append(item)
                for pending_item in pending:
                    pending_item[1].cancel()
            remaining = len(self._threads)
            while remaining:
                self._queue.put(None)
                remaining -= 1
        # Join outside the lock: a worker blocked in queue.get() only needs
        # the sentinel, and join must not run while submit is waiting.
        if wait:
            _join_pool_workers(self._threads)


def _join_pool_workers(threads: list[threading.Thread]) -> None:
    """Join each pool worker. Do not return while it is still alive.

    Keep joining. ``join(timeout=5.0)`` once, then dropping the pool,
    treats the timeout as "joined". Under load the job already dequeued
    outlasts 5s, the sentinel sits until that job ends, and the thread
    keeps running as ``wa-bg-retired-*`` next to the next pool.
    ``_wait_for_exit`` uses no timeout for the same reason (a reader is
    not done until it returns). Slices stay interruptible. A job that
    never returns blocks shutdown instead of leaving a live worker.
    """
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=_POOL_SHUTDOWN_JOIN_SLICE_SEC)


def _get_pool() -> _DaemonWorkPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DaemonWorkPool(background_pool_max_workers())
        return _pool


def reset_background_pool_for_tests(max_workers: int | None = None) -> None:
    """Tear down the process pool so tests can bound size or avoid leaked work.

    Detach the pool under the lock, then join it after the lock is
    released. ``shutdown`` joining workers while this function holds
    ``_pool_lock`` deadlocks: a pooled job that calls ``run_in_background``
    takes that lock in ``_get_pool``, so reset waits on the job and the
    job waits on reset.
    """
    global _pool, _pool_size_override
    with _pool_lock:
        old = _pool
        _pool = None
        _pool_size_override = max_workers
    if old is not None:
        old.shutdown(wait=True, cancel_futures=True)


def run_in_background(func: Callable[..., Any], *args: Any, name: str | None = None, error_callback: Callable[[WorkerPoolError], None] | None = None, daemon: bool = True, dedicated: bool = False, **kwargs: Any) -> BackgroundHandle:
    """Run *func* off the caller thread with WorkerPoolError isolation and Layer A tagging.

    Short fire-and-forget work is queued on a daemon pool with a fixed worker
    count (unbounded submit queue). Pass ``dedicated=True`` (or ``daemon=False``)
    for servers, pipe drains, infinite loops, and any job another thread will
    ``join()`` — those must not occupy a pool slot.

    Without *error_callback*, failures are logged only; ``BackgroundHandle.join()``
    does not re-raise the worker exception.

    :return: A :class:`BackgroundHandle` with ``join`` / ``is_alive``.
    """

    # Stop cancellation is a contextvar on the send thread. Pool and dedicated
    # workers did not inherit it, so marshal items they enqueued had scope None
    # and survived cancel_pending_work.
    caller_ctx = contextvars.copy_context()

    def _worker() -> Any:
        task_id = str(uuid.uuid4())
        task_name = name or getattr(func, "__name__", "anon")
        log.debug(f"Starting task {task_id}: {task_name}")

        # Tag this background thread for the UNO thread-safety guard (Layer A).
        # This lets violations report the specific worker (e.g. "run_search") instead of a generic thread name.
        thread_guard.set_background_task(task_name)

        try:
            result = func(*args, **kwargs)
            log.debug(f"Task {task_id} completed successfully")
            return result
        except Exception as e:
            # Not BaseException: SystemExit/KeyboardInterrupt must still unwind on
            # dedicated threads. Pool _run catches BaseException to finish the Future.
            error_id = str(uuid.uuid4())
            log.exception(f"Task {task_id} failed", extra={"task_id": task_id, "task_name": task_name, "error_id": error_id, "error_type": type(e).__name__})

            wrapped_error = WorkerPoolError(f"Task '{task_name}' failed", code="WORKER_TASK_FAILED", details={"task_id": task_id, "task_name": task_name, "error_id": error_id, "original_error": str(e), "error_type": type(e).__name__})

            if error_callback:
                try:
                    error_callback(wrapped_error)
                except Exception:
                    log.exception("Error in error_callback for '%s'", task_name)
        finally:
            thread_guard.set_background_task(None)

    def _run_in_caller_context() -> Any:
        return caller_ctx.run(_worker)

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
    try:
        if callable(read1):
            chunk = read1(4096)
            if isinstance(chunk, (bytes, str)) or chunk is None:
                return chunk
            return None
        return readable.read(4096)
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
                pass

    thread = run_in_background(_loop, name=name, dedicated=True)
    tail.attach_thread(thread)
    return tail


class AsyncProcess:
    """
    Manages a subprocess.Popen instance, asynchronously reading its stdout/stderr
    streams and providing a callback mechanism for output and exit.
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
        # Pipes stay binary. text=True is ignored; see the popen kwargs below.
        self.process: Optional[subprocess.Popen[Any]] = None

        # Copy: setdefault must not mutate the caller's dict.
        self._popen_kwargs: dict[str, Any] = dict(popen_kwargs)
        if sys.platform == "win32":
            self._popen_kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        self._popen_kwargs.setdefault("stdout", subprocess.PIPE)
        self._popen_kwargs.setdefault("stderr", subprocess.PIPE)
        # Force binary pipes. text=True was documented as keeping the
        # caller's encoding, but _read_stream reads the binary buffer and
        # decodes UTF-8. A TextIOWrapper plus that read desyncs the two
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

        self._stdout_thread: BackgroundHandle | None = None
        self._stderr_thread: BackgroundHandle | None = None
        self._wait_thread: BackgroundHandle | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        """Starts the process and its monitoring threads."""
        if self.process is not None and self.process.poll() is None:
            # A second start() used to leak the previous Popen. terminate()
            # returns before the child is dead so it cannot join callback
            # threads on this stack. Wait here, without those joins.
            old = self.process
            self.terminate()
            try:
                old.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("Previous process still alive after terminate")
        try:
            proc: subprocess.Popen[Any] = subprocess.Popen(self.args, **self._popen_kwargs)
        except Exception as e:
            log.exception("Failed to start process: %s", self.args)
            from plugin.framework.errors import ToolExecutionError

            raise ToolExecutionError(f"Failed to start process: {self.args}", details={"error": str(e)}) from e

        self.process = proc
        stdout_thread: BackgroundHandle | None = None
        stderr_thread: BackgroundHandle | None = None
        if proc.stdout and self.stdout_cb:
            stdout_thread = run_in_background(self._read_stream, proc.stdout, self.stdout_cb, name=f"asyncproc-out-{proc.pid}", dedicated=True)
        elif proc.stdout:
            # Drain it silently to avoid deadlocks
            stdout_thread = run_in_background(self._drain_stream, proc.stdout, name=f"asyncproc-outdrain-{proc.pid}", dedicated=True)

        if proc.stderr and self.stderr_cb:
            stderr_thread = run_in_background(self._read_stream, proc.stderr, self.stderr_cb, name=f"asyncproc-err-{proc.pid}", dedicated=True)
        elif proc.stderr:
            stderr_thread = run_in_background(self._drain_stream, proc.stderr, name=f"asyncproc-errdrain-{proc.pid}", dedicated=True)

        self._stdout_thread = stdout_thread
        self._stderr_thread = stderr_thread
        # Pass this child and these readers. _wait_for_exit must not read
        # self.process later: a second start() replaces them.
        self._wait_thread = run_in_background(self._wait_for_exit, proc, stdout_thread, stderr_thread, name=f"asyncproc-wait-{proc.pid}", dedicated=True)

    def _read_stream(self, stream: Any, callback: Any) -> None:
        # ``for line in stream`` blocks in readline. A child that writes a long
        # burst with no newline fills the OS pipe and deadlocks. A callback
        # exception used to leave that loop and close the pipe while the child
        # was still writing. Short reads go through ``_iter_decoded_chunks`` so
        # a UTF-8 code point split across reads is not replaced twice. The
        # callback try/except stays inside the loop: a bad callback must not
        # close the pipe early.
        pending = ""
        try:
            for chunk in _iter_decoded_chunks(stream):
                pending += chunk
                lines, pending = _split_on_newlines(pending)
                for line in lines:
                    try:
                        callback(line.rstrip("\n\r"))
                    except Exception:
                        log.exception("AsyncProcess stream callback failed")
        finally:
            if pending:
                try:
                    callback(pending.rstrip("\n\r"))
                except Exception:
                    log.exception("AsyncProcess stream callback failed")
            try:
                stream.close()
            except OSError:
                pass

    def _drain_stream(self, stream: Any) -> None:
        try:
            for _text in _iter_decoded_chunks(stream):
                pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

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
            # raises from a wa-bg-* worker so the pool cannot deadlock.
            if handle.is_current_thread():
                continue
            handle.join(timeout=timeout)

    def _close_child_pipes(self) -> None:
        proc = self.process
        if proc is None:
            return
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _wait_for_exit(self, proc: subprocess.Popen[Any], stdout_thread: BackgroundHandle | None, stderr_thread: BackgroundHandle | None) -> None:
        # Wait and join only this call's child. Reading self.process and
        # the reader handles at wait time follows start() if it runs again
        # and points those at a new child, so the first wait thread blocks
        # on that child and fires on_exit_cb for its exit as well. _reap
        # already captures the handles it was given.
        rc = proc.wait()
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
        if self.on_exit_cb:
            try:
                self.on_exit_cb(rc)
            except Exception:
                log.exception("Error in on_exit_cb for process")

    def _reap(self, proc: subprocess.Popen[Any], stdout_thread: BackgroundHandle | None, stderr_thread: BackgroundHandle | None, wait_thread: BackgroundHandle | None, timeout: float) -> None:
        """Wait, then SIGKILL, off the caller stack. See terminate().

        Handles are captured at terminate() time. A later start() replaces
        self.process; this reaper must not wait on that new child.
        """
        if proc.poll() is None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                # A stuck SIGKILL (uninterruptible sleep) must not block forever.
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    log.warning("Process still alive after kill (timeout=%ss)", timeout)
        self._join_handles((stdout_thread, stderr_thread, wait_thread))

    def terminate(self, timeout: float = 5.0) -> None:
        """Signal the child and return. Reap on a dedicated thread.

        Signal the child, close its pipes so reads return, and reap after
        the caller can drop its lock. Waiting up to 5s twice, then joining
        the stdout, stderr, and wait threads, freezes LibreOffice: MCP
        tunnel stop calls this while holding the lock those line callbacks
        need, including from the UI thread. A callback that called
        terminate joined itself. The timed wait stays on the reaper so a
        stuck SIGKILL cannot block forever.
        """
        proc = self.process
        if proc is None:
            return
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread
        wait_thread = self._wait_thread
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.debug("terminate: process already gone", exc_info=True)
        self._close_child_pipes()
        run_in_background(self._reap, proc, stdout_thread, stderr_thread, wait_thread, timeout, name="asyncproc-reap", dedicated=True)
