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
forever. ``_pool_lock`` only covers creating/resetting that pool;
submitting work uses a thread-safe queue. ``StderrTail``’s lock is only
the bounded stderr buffer from a child process. Never ``join()`` a pooled
job from another pooled job (the pool can deadlock). Map:
docs/framework/threading.md.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections import deque
from concurrent.futures import CancelledError, Future, TimeoutError as FuturesTimeoutError
from typing import Optional, Callable, Any, IO

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

    __slots__ = ("_future", "_thread")

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
        try:
            fut.result(timeout=timeout)
        except FuturesTimeoutError:
            return
        except CancelledError:
            # 3.9+: concurrent.futures.CancelledError is BaseException, not Exception.
            return
        except Exception:
            # Do not catch BaseException: KeyboardInterrupt/SystemExit from
            # fut.set_exception must still escape join().
            return

    def is_alive(self) -> bool:
        thread = self._thread
        if thread is not None:
            return thread.is_alive()
        fut = self._future
        return fut is not None and not fut.done()


class _DaemonWorkPool:
    """Fixed daemon workers + unbounded queue. ThreadPoolExecutor is non-daemon on 3.9+.

    SimpleQueue has no maxsize on purpose: a bounded queue would block or drop
    fire-and-forget UI work. Bound the *worker count*, not the submit queue.
    """

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max(1, max_workers)
        self._queue: queue.SimpleQueue[tuple[Callable[[], None], Future[Any]] | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False
        for i in range(self._max_workers):
            t = threading.Thread(target=self._run, name=f"wa-bg-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Callable[[], None]) -> Future[Any]:
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
        if wait:
            for t in self._threads:
                t.join(timeout=5.0)


def _get_pool() -> _DaemonWorkPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DaemonWorkPool(background_pool_max_workers())
        return _pool


def reset_background_pool_for_tests(max_workers: int | None = None) -> None:
    """Tear down the process pool so tests can bound size or avoid leaked work."""
    global _pool, _pool_size_override
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
            _pool = None
        _pool_size_override = max_workers


def run_in_background(
    func: Callable[..., Any],
    *args: Any,
    name: str | None = None,
    error_callback: Callable[[WorkerPoolError], None] | None = None,
    daemon: bool = True,
    dedicated: bool = False,
    **kwargs: Any,
) -> BackgroundHandle:
    """Run *func* off the caller thread with WorkerPoolError isolation and Layer A tagging.

    Short fire-and-forget work is queued on a daemon pool with a fixed worker
    count (unbounded submit queue). Pass ``dedicated=True`` (or ``daemon=False``)
    for servers, pipe drains, infinite loops, and any job another thread will
    ``join()`` — those must not occupy a pool slot.

    Without *error_callback*, failures are logged only; ``BackgroundHandle.join()``
    does not re-raise the worker exception.

    :return: A :class:`BackgroundHandle` with ``join`` / ``is_alive``.
    """

    def _worker():
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

    use_dedicated = dedicated or (daemon is False)
    if use_dedicated:
        thread_name = name or f"worker-{getattr(func, '__name__', 'anon')}"
        t = threading.Thread(target=_worker, name=thread_name, daemon=daemon)
        t.start()
        return BackgroundHandle(thread=t)

    fut = _get_pool().submit(_worker)
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

    __slots__ = ("_lock", "_chunks", "_chars", "_max_chars", "_thread")

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


def start_stderr_drain(
    stream: IO[Any] | None,
    *,
    max_tail_chars: int = _DEFAULT_STDERR_TAIL_CHARS,
    name: str = "stderr-drain",
) -> StderrTail | None:
    """Continuously drain a child stderr pipe into a bounded :class:`StderrTail`.

    Call this immediately after ``Popen(..., stderr=PIPE)`` for long-lived workers.
    Returns None when *stream* is None (e.g. stderr redirected to DEVNULL).
    """
    if stream is None:
        return None
    tail = StderrTail(max_chars=max_tail_chars)

    def _loop() -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    text = chunk.decode("utf-8", errors="replace")
                else:
                    text = chunk
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

    def __init__(
        self,
        args: str | list[str],
        stdout_cb: Optional[Callable[[str], None]] = None,
        stderr_cb: Optional[Callable[[str], None]] = None,
        on_exit_cb: Optional[Callable[[int], None]] = None,
        **popen_kwargs: Any,
    ) -> None:
        self.args = args
        self.stdout_cb = stdout_cb
        self.stderr_cb = stderr_cb
        self.on_exit_cb = on_exit_cb
        self.process: Optional[subprocess.Popen[str]] = None

        self._popen_kwargs: dict[str, Any] = popen_kwargs
        if sys.platform == "win32":
            self._popen_kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        self._popen_kwargs.setdefault("stdout", subprocess.PIPE)
        self._popen_kwargs.setdefault("stderr", subprocess.PIPE)
        self._popen_kwargs.setdefault("text", True)
        self._popen_kwargs.setdefault("bufsize", 1)  # Line buffered

        self._stdout_thread: BackgroundHandle | None = None
        self._stderr_thread: BackgroundHandle | None = None
        self._wait_thread: BackgroundHandle | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        """Starts the process and its monitoring threads."""
        try:
            self.process = subprocess.Popen(self.args, **self._popen_kwargs)
        except Exception as e:
            log.exception("Failed to start process: %s", self.args)
            from plugin.framework.errors import ToolExecutionError

            raise ToolExecutionError(f"Failed to start process: {self.args}", details={"error": str(e)}) from e

        if self.process.stdout and self.stdout_cb:
            self._stdout_thread = run_in_background(self._read_stream, self.process.stdout, self.stdout_cb, name=f"asyncproc-out-{self.process.pid}", dedicated=True)
        elif self.process.stdout:
            # Drain it silently to avoid deadlocks
            run_in_background(self._drain_stream, self.process.stdout, name=f"asyncproc-outdrain-{self.process.pid}", dedicated=True)

        if self.process.stderr and self.stderr_cb:
            self._stderr_thread = run_in_background(self._read_stream, self.process.stderr, self.stderr_cb, name=f"asyncproc-err-{self.process.pid}", dedicated=True)
        elif self.process.stderr:
            run_in_background(self._drain_stream, self.process.stderr, name=f"asyncproc-errdrain-{self.process.pid}", dedicated=True)

        self._wait_thread = run_in_background(self._wait_for_exit, name=f"asyncproc-wait-{self.process.pid}", dedicated=True)

    def _read_stream(self, stream: Any, callback: Any) -> None:
        try:
            for line in stream:
                if line is not None:
                    callback(line.rstrip("\n\r"))
        except ValueError:
            pass  # ValueError: I/O operation on closed file
        except OSError as e:
            log.debug("AsyncProcess stream read error: %s", e)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _drain_stream(self, stream: Any) -> None:
        try:
            for _unused in stream:
                pass
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait_for_exit(self):
        if self.process is None:
            return
        rc = self.process.wait()
        # argv preview for the log line; a str args is the first character.
        log.debug("Process %s exited with rc=%s", self.args[0] if getattr(self.args, "__len__", lambda: 0)() > 0 else self.args, rc)
        if self.on_exit_cb:
            try:
                self.on_exit_cb(rc)
            except Exception:
                log.exception("Error in on_exit_cb for process")

    def terminate(self, timeout: float = 5.0) -> None:
        """Standard graceful termination -> SIGKILL."""
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
