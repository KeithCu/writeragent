# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side helpers for venv microphone recording and chat audio.

A Recording owns one child's lifecycle. Chat also uses it to attach a finished
WAV as ``input_audio``.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, Any, cast

from plugin.framework.config import get_config
from plugin.framework.managed_subprocess import ManagedSubprocess, kill_process_tree
from plugin.framework.worker_pool import BackgroundHandle, run_in_background
from plugin.scripting.audio_silence_detector import (
    RECORDER_COMMAND_STOP,
    RECORDER_STATUS_AUTO_STOPPED,
    RECORDER_STATUS_ERROR,
    RECORDER_STATUS_OK,
    RECORDER_STATUS_READY,
    RECORDER_STATUS_SILENCE_PROGRESS,
)
from plugin.scripting.native_binaries import ensure_native_binaries_on_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
from plugin.framework.ipc import read_json_line, write_json_line
from plugin.framework.subprocess_env import scrub_subprocess_env
from plugin.scripting.sandbox import resolve_venv_python, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_AUDIO_RECORD_MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "audio_record_main.py")
_RECORDING_READY_TIMEOUT_SEC = 30
_RECORDING_STOP_TIMEOUT_SEC = 15
_RECORDING_REAP_TIMEOUT_SEC = 2.0
_RECORDING_MONITOR_JOIN_SEC = 1.0

_VENV_NOT_CONFIGURED = "Set the Python venv path in WriterAgent Settings → Python, then run 'uv pip install sounddevice' in that venv."


def is_audio_recording_configured() -> bool:
    """True when Settings → Python points at a venv with a python executable."""
    return resolve_recording_python()[0] is not None


def resolve_recording_python() -> tuple[str | None, str]:
    """Return (venv python executable, error message)."""
    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
    if not venv_dir:
        return None, _VENV_NOT_CONFIGURED
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (None, f"No python executable found under configured venv: {venv_dir!r} (expected bin/python, Scripts/python.exe, or env-root python.exe).")
    return exe, ""


def spawn_recording_process(exe: str, output_path: str, *, silence_config: SilenceDetectorConfig | None = None) -> Recording:
    """Start audio_record_main.py in the user venv.

    The return value owns the process, monitor and stop handoff.
    """
    cmd = [exe, _AUDIO_RECORD_MAIN, "--output", output_path]
    if silence_config is not None:
        cmd.append(f"--silence-stop-ms={max(0, silence_config.silence_stop_ms)}")
    # text=True: read_json_line needs a text pipe, and bufsize=1 is what this
    # child already used. Worker start() is binary bufsize=0.
    # base_env is the scrubbed environment. base_env=None would copy
    # os.environ and leak the vars scrub_subprocess_env drops.
    # register=False: shutdown_all kill_trees the registry and would skip the
    # stop JSON. This child was never in that registry.
    # log_start_failure=False: a missing interpreter must stay an OSError so
    # the sidebar can prefix "Venv audio recording failed to start".
    # stderr_name_with_pid: the drain thread stays audio-rec-stderr-<pid>.
    # ManagedSubprocess.start calls the wrapper as wrapper(cmd, env=..., cwd=...)
    # and does not pass watch_bus, so the default is false; kill_process_tree
    # then signals flatpak-spawn and the host Python keeps the microphone until
    # MAX_RECORDING_DURATION_SEC (4 hours). --watch-bus ties that host child to
    # the caller bus, including when the bus drops. That is the same choice as
    # stt_service._run_cmd.
    managed = ManagedSubprocess(
        cmd,
        base_env=scrub_subprocess_env(dict(os.environ)),
        command_wrapper=lambda cmd, env=None, cwd=None: wrap_command_for_sandbox(cmd, env=env, cwd=cwd, watch_bus=True),
        text=True,
        register=False,
        log_start_failure=False,
        stderr_name="audio-rec-stderr",
        stderr_name_with_pid=True,
        label="audio-recorder",
    )
    managed.start()
    return Recording(managed)


class RecordingStopHandoff:
    """WAV path stashed by the sole stdout monitor for ``Recording.finish``.

    The stdout monitor owns the JSON lines so Manual Stop shares the ok/path handoff instead of racing a second reader on the pipe.
    """

    _lock: threading.Lock
    _ready: threading.Event
    _monitor_stop: threading.Event
    _path: str | None
    _error: str | None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._monitor_stop = threading.Event()
        self._path = None
        self._error = None

    def is_settled(self) -> bool:
        """True once note_ok or note_error ran."""
        return self._ready.is_set()

    def request_monitor_stop(self) -> None:
        self._monitor_stop.set()

    def monitor_stop_requested(self) -> bool:
        return self._monitor_stop.is_set()

    def note_ok(self, path: str) -> None:
        """Record a finished WAV from ``ok`` or ``auto_stopped``."""
        if not path:
            return
        with self._lock:
            if self._path is None:
                self._path = path
        self._ready.set()

    def note_error(self, message: str) -> None:
        """Record a terminal child error so stop does not wait out the timeout."""
        with self._lock:
            if self._error is None:
                self._error = message or "Audio recording failed."
        self._ready.set()

    def snapshot_path(self) -> str | None:
        with self._lock:
            return self._path

    def wait_for_path(self, timeout_sec: float) -> str:
        """Block until ``note_ok`` / ``note_error``, or raise on timeout."""
        if not self._ready.wait(timeout_sec):
            raise RuntimeError(f"Recording subprocess timed out after {timeout_sec:g} seconds.")
        with self._lock:
            # A path wins over an error: auto-stop can emit both, and the WAV is usable.
            if self._path:
                return self._path
            if self._error:
                raise RuntimeError(self._error)
        raise RuntimeError("Recording subprocess did not return a WAV path.")


class Recording:
    """One venv recorder child: its process, the sole stdout monitor, and the stop handoff."""

    managed: ManagedSubprocess
    popen: subprocess.Popen[str]
    handoff: RecordingStopHandoff
    monitor: BackgroundHandle | None

    def __init__(self, managed: ManagedSubprocess) -> None:
        proc = cast("subprocess.Popen[str] | None", managed.popen)
        if proc is None:
            raise RuntimeError("Recording subprocess is not running.")
        self.managed = managed
        self.popen = proc
        self.handoff = RecordingStopHandoff()
        self.monitor = None

    @property
    def monitor_started(self) -> bool:
        return self.monitor is not None

    def is_alive(self) -> bool:
        return self.managed.is_alive()

    def wait_ready(self, *, timeout_sec: float | None = None) -> None:
        """Block until the child emits ``{"status": "ready"}`` or fails."""
        if timeout_sec is None:
            timeout_sec = _RECORDING_READY_TIMEOUT_SEC
        proc = self.popen
        if proc.stdout is None:
            raise RuntimeError("Recording subprocess stdout is not available.")
        # stdout.readline() ignores the ready/stop timeout when the child hangs
        # before emitting JSON. The shared IPC helper waits with a real deadline
        # before reading the line.
        try:
            payload = read_json_line(proc.stdout, timeout_sec=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Recording subprocess timed out after {timeout_sec:g} seconds.") from exc
        except ValueError as exc:
            raise RuntimeError(f"Invalid recording subprocess response: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to read from recording subprocess: {exc}") from exc
        if payload is None:
            drain = self.managed.stderr_tail
            stderr = (drain.finish_text() if drain is not None else "") or ""
            code = proc.poll()
            detail = stderr.strip() or f"exit code {code}"
            raise RuntimeError(f"Recording subprocess ended before responding ({detail}).")
        status = payload.get("status")
        if status == RECORDER_STATUS_READY:
            return
        if status == RECORDER_STATUS_ERROR:
            raise RuntimeError(str(payload.get("message") or "Audio recording failed to start."))
        raise RuntimeError(f"Unexpected recording subprocess status: {status!r}")

    def _reap(self, timeout_sec: float) -> None:
        """Wait, then escalate to the process group. The drain is stderr_tail, joined only after this sequence, because joining it earlier filled stderr and stalled the child.

        ``terminate`` alone left a PortAudio thread holding the mic, and dropping
        the drain at the same time filled the stderr pipe and stalled the child.
        """
        proc = self.popen
        for wait_sec, escalate in ((timeout_sec, proc.terminate), (1.0, lambda: kill_process_tree(proc)), (2.0, None)):
            try:
                proc.wait(timeout=wait_sec)
                break
            except subprocess.TimeoutExpired:
                if escalate is None:
                    log.warning("audio recorder pid=%s still alive after kill", proc.pid)
                else:
                    escalate()
        # Closing stdout unblocks a read, and joining first is what keeps that
        # close off the common path; the fatal ValueError remains for a join that
        # times out.
        self.handoff.request_monitor_stop()
        if self.monitor is not None and not self.monitor.is_current_thread():
            self.monitor.join(timeout=_RECORDING_MONITOR_JOIN_SEC)
        drain = self.managed.stderr_tail
        if drain is not None:
            drain.join(timeout=1.0)
        # Close recording pipes when reaping so monitor threads are not stuck on os.read and fds are not held until GC.
        _close_recording_pipe(proc.stdin, "stdin")
        _close_recording_pipe(proc.stdout, "stdout")
        _close_recording_pipe(proc.stderr, "stderr")

    def signal_stop(self) -> None:
        """Send {"command": "stop"} to the recording subprocess if still running.

        Does not wait for the process to exit or reap it.
        """
        proc = self.popen
        if proc.poll() is not None:
            return
        if proc.stdin is None:
            raise RuntimeError("Recording subprocess stdin is not available.")
        try:
            write_json_line(proc.stdin, {"command": RECORDER_COMMAND_STOP})
        except OSError as exc:
            raise RuntimeError(f"Failed to signal recording subprocess: {exc}") from exc

    def finish(self, *, timeout_sec: float | None = None) -> str:
        """Wait on the handoff for the WAV path, then reap the recording process.

        No second stop line is written. Call after start_monitor; without a
        monitor no line reaches the handoff and this waits out the timeout.
        """
        if timeout_sec is None:
            timeout_sec = _RECORDING_STOP_TIMEOUT_SEC
        proc = self.popen
        try:
            exited = proc.poll() is not None
            try:
                if exited:
                    return self.handoff.wait_for_path(min(timeout_sec, 1.0))
                return self.handoff.wait_for_path(timeout_sec)
            except RuntimeError:
                known = self.handoff.snapshot_path()
                if known:
                    return known
                if exited:
                    raise RuntimeError(
                        "Recording subprocess already exited without a WAV path."
                    ) from None
                raise
        finally:
            # Reap on every exit (success, timeout, or exception) so the process is never leaked.
            self._reap(_RECORDING_REAP_TIMEOUT_SEC)

    def start_monitor(
        self,
        *,
        on_auto_stopped: Callable[[str], None],
        on_silence_progress: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Sole stdout reader for venv recorder IPC.

        This thread consumes ``silence_progress``, ``auto_stopped``, ``error``,
        and the final ``ok`` line. Stop must not read the pipe or it races
        this loop and loses the path.
        """
        if self.monitor is not None:
            raise RuntimeError("Recording stdout monitor already started.")
        proc = self.popen
        handoff = self.handoff

        def _reader() -> None:
            if proc.stdout is None:
                return
            # Read until EOF so an ok line written as the child exits is not missed before the monitor stops.
            while True:
                if handoff.monitor_stop_requested():
                    break
                try:
                    payload = read_json_line(proc.stdout, timeout_sec=0.25)
                except subprocess.TimeoutExpired:
                    if proc.poll() is not None:
                        break
                    continue
                except (ValueError, RuntimeError, OSError) as exc:
                    # Closed-pipe and oversize-line ValueErrors end the monitor loop; other ValueErrors skip one bad JSON line.
                    if isinstance(exc, ValueError) and not str(exc).startswith((
                        "I/O operation on closed file",
                        "JSON line exceeds ",
                        "JSON line truncated at EOF",
                    )):
                        log.warning("Skipping non-JSON line from recording subprocess: %s", exc)
                        continue
                    log.debug("Recording IPC monitor stopped: %s", exc)
                    break
                if payload is None:
                    # EOF after the child exited without an ok or error frame (a
                    # crash): report it and wake a Stop waiting on the handoff
                    # instead of leaving it to time out.
                    if proc.poll() is not None and not handoff.is_settled():
                        message = "Recording subprocess exited unexpectedly."
                        handoff.note_error(message)
                        if on_error is not None:
                            try:
                                on_error(message)
                            except Exception:
                                log.exception("Recording error callback failed")
                    break
                try:
                    _dispatch_recording_stdout(payload, handoff=handoff, on_auto_stopped=on_auto_stopped, on_silence_progress=on_silence_progress, on_error=on_error)
                except Exception:
                    log.exception("Recording stdout callback failed")

        self.monitor = run_in_background(_reader, name="audio-rec-stdout-monitor", daemon=True, dedicated=True)

    def terminate(self) -> None:
        """Best-effort shutdown of a recording child."""
        proc = self.popen
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    write_json_line(proc.stdin, {"command": RECORDER_COMMAND_STOP})
            except OSError:
                pass
        self._reap(_RECORDING_REAP_TIMEOUT_SEC)

    def kill_tree(self) -> None:
        """Calls kill_process_tree directly, not ManagedSubprocess.kill_tree, so Stop-during-start keeps the error path it had (no swallow-and-log arm)."""
        kill_process_tree(self.popen)


def _close_recording_pipe(stream: Any, label: str) -> None:
    if stream is not None:
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            log.debug("Failed to close %s pipe: %s", label, exc)


def _dispatch_recording_stdout(
    payload: dict[str, Any],
    *,
    handoff: RecordingStopHandoff,
    on_auto_stopped: Callable[[str], None],
    on_silence_progress: Callable[[int], None] | None,
    on_error: Callable[[str], None] | None,
) -> None:
    """Handle one child IPC line. Stash ``ok`` before any callback that may stop.

    Callbacks run on this thread. ``note_ok`` / ``note_error`` must happen
    first: a synchronous stop waits on the handoff, and it would deadlock if
    the path were published only after the callback returned.
    """
    status = payload.get("status")
    if status in {RECORDER_STATUS_OK, RECORDER_STATUS_AUTO_STOPPED}:
        raw_path = payload.get("path")
        if isinstance(raw_path, str) and raw_path:
            handoff.note_ok(raw_path)
            if status == RECORDER_STATUS_AUTO_STOPPED:
                on_auto_stopped(raw_path)
        return
    if status == RECORDER_STATUS_SILENCE_PROGRESS and on_silence_progress is not None:
        ms = payload.get("ms")
        if isinstance(ms, int):
            on_silence_progress(ms)
        return
    if status == RECORDER_STATUS_ERROR:
        raw_msg = payload.get("message")
        message = raw_msg if isinstance(raw_msg, str) and raw_msg else "Audio recording failed."
        # note_error always runs so wait_for_path does not block the full stop timeout when on_error is omitted.
        handoff.note_error(message)
        if on_error is not None:
            on_error(message)
        return


def make_temp_wav_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def is_audio_recording_supported() -> bool:
    """True when either the user venv is configured, or host-side audio libraries are installed."""
    if is_audio_recording_configured():
        return True
    ensure_native_binaries_on_path()
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        return any(d.get("max_input_channels", 0) > 0 for d in devices)
    except Exception:
        return False


def append_wav_as_input_audio(content_list: list[dict[str, Any]], wav_path: str) -> bool:
    """Read a WAV and append an OpenAI ``input_audio`` content part.

    Returns False when the file cannot be read. The caller clears
    ``audio_wav_path`` and still posts the text or image message.
    """
    try:
        with open(wav_path, "rb") as f:
            wav_data = f.read()
        b64_audio = base64.b64encode(wav_data).decode("utf-8")
        content_list.append({"type": "input_audio", "input_audio": {"data": b64_audio, "format": "wav"}})
        return True
    except OSError:
        log.exception("Audio file error")
        log.debug("Audio file preserved at: %s", wav_path)
        return False
    except Exception:
        log.exception("Unexpected audio error")
        return False


def clear_pending_audio_wav(host: Any) -> None:
    """Clear and delete any un-sent audio recording."""
    if host.audio_wav_path:
        try:
            os.remove(host.audio_wav_path)
        except OSError as exc:
            # Log OSError on WAV delete (common on Windows when the child still holds the file) but clear the pending path reference.
            log.debug("clear_pending_audio_wav: failed to remove %s: %s", host.audio_wav_path, exc)
        host.audio_wav_path = None
