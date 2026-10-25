# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side helpers for venv microphone recording and chat audio.

Spawn goes through ManagedSubprocess and the stop/reap sequence stays here. Chat
also uses it to attach a finished WAV as ``input_audio``.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, Any, cast

from plugin.framework.config import get_config_str
from plugin.framework.managed_subprocess import ManagedSubprocess, kill_process_tree
from plugin.framework.worker_pool import BackgroundHandle, run_in_background
from plugin.scripting.native_binaries import ensure_native_binaries_on_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
from plugin.scripting.ipc import read_json_line, write_json_line
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_AUDIO_RECORD_MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "audio_record_main.py")
_RECORDING_READY_TIMEOUT_SEC = 30
_RECORDING_STOP_TIMEOUT_SEC = 15
_RECORDING_REAP_TIMEOUT_SEC = 2.0
_RECORDING_MONITOR_JOIN_SEC = 1.0

_VENV_NOT_CONFIGURED = "Set the Python venv path in WriterAgent Settings → Python, then run 'uv pip install sounddevice' in that venv."


def is_audio_recording_configured(ctx: Any) -> bool:
    """True when Settings → Python points at a venv with a python executable."""
    del ctx
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    return resolve_venv_python(venv_dir) is not None


def resolve_recording_python(ctx: Any) -> tuple[str | None, str]:
    """Return (venv python executable, error message)."""
    del ctx
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None, _VENV_NOT_CONFIGURED
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (None, f"No python executable found under configured venv: {venv_dir!r} (expected bin/python, Scripts/python.exe, or env-root python.exe).")
    return exe, ""


def _build_recording_env() -> dict[str, str]:
    env = scrub_subprocess_env(dict(os.environ))
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def _silence_cli_args(config: SilenceDetectorConfig) -> list[str]:
    return [f"--silence-stop-ms={max(0, config.silence_stop_ms)}"]


def _recording_popen(managed: ManagedSubprocess) -> subprocess.Popen[str]:
    proc = cast("subprocess.Popen[str] | None", managed.popen)
    if proc is None:
        raise RuntimeError("Recording subprocess is not running.")
    return proc


def _wrap_recording_command(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> list[str]:
    return wrap_command_for_sandbox(cmd, env=env, cwd=cwd, watch_bus=True)


def spawn_recording_process(exe: str, output_path: str, *, silence_config: SilenceDetectorConfig | None = None) -> ManagedSubprocess:
    """Start audio_record_main.py in the user venv.

    The return value is the ManagedSubprocess; stop and reap still use its Popen.
    """
    cmd = [exe, _AUDIO_RECORD_MAIN, "--output", output_path]
    if silence_config is not None:
        cmd.extend(_silence_cli_args(silence_config))
    # text=True: read_json_line needs a text pipe, and bufsize=1 is what this
    # child already used. Worker start() is binary bufsize=0.
    # base_env is already scrubbed, with the display and Pulse keys put back.
    # A second scrub, or env= merged onto os.environ, would undo that.
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
        base_env=_build_recording_env(),
        command_wrapper=_wrap_recording_command,
        text=True,
        register=False,
        log_start_failure=False,
        stderr_name="audio-rec-stderr",
        stderr_name_with_pid=True,
        label="audio-recorder",
    )
    managed.start()
    return managed


class RecordingStopHandoff:
    """WAV path stashed by the sole stdout monitor for ``stop_recording_process``.

    Manual Stop Rec used to call ``read_json_line`` on the same pipe the silence
    monitor was already reading. The monitor only handled ``silence_progress``,
    ``auto_stopped``, and ``error``, so it dropped ``{"status":"ok","path":…}``.
    Stop then timed out, and the panel deleted the temp WAV and sent nothing.
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

    def has_error(self) -> bool:
        with self._lock:
            return self._error is not None

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


def _read_json_line(managed: ManagedSubprocess, timeout: float) -> dict[str, Any]:
    proc = _recording_popen(managed)
    if proc.stdout is None:
        raise RuntimeError("Recording subprocess stdout is not available.")
    # stdout.readline() ignores the ready/stop timeout when the child hangs
    # before emitting JSON. The shared IPC helper waits with a real deadline
    # before reading the line.
    try:
        payload = read_json_line(proc.stdout, timeout_sec=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Recording subprocess timed out after {timeout:g} seconds.") from exc
    except ValueError as exc:
        raise RuntimeError(f"Invalid recording subprocess response: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to read from recording subprocess: {exc}") from exc
    if payload is None:
        drain = managed.stderr_tail
        stderr = (drain.finish_text() if drain is not None else "") or ""
        code = proc.poll()
        detail = stderr.strip() or f"exit code {code}"
        raise RuntimeError(f"Recording subprocess ended before responding ({detail}).")
    return payload


def wait_for_recording_ready(managed: ManagedSubprocess, *, timeout_sec: float = _RECORDING_READY_TIMEOUT_SEC) -> None:
    """Block until the child emits ``{\"status\": \"ready\"}`` or fails."""
    payload = _read_json_line(managed, timeout_sec)
    status = payload.get("status")
    if status == "ready":
        return
    if status == "error":
        raise RuntimeError(str(payload.get("message") or "Audio recording failed to start."))
    raise RuntimeError(f"Unexpected recording subprocess status: {status!r}")


def _close_recording_pipe(stream: Any, label: str) -> None:
    if stream is None:
        return
    fileno_fn = getattr(stream, "fileno", None)
    if not callable(fileno_fn):
        return
    try:
        fd = fileno_fn()
    except (OSError, ValueError):
        return
    if not isinstance(fd, int) or isinstance(fd, bool):
        return
    try:
        stream.close()
    except (OSError, ValueError) as exc:
        log.debug("Failed to close %s pipe: %s", label, exc)


def _reap_recording_process(
    managed: ManagedSubprocess,
    timeout_sec: float,
    *,
    monitor: BackgroundHandle | None = None,
    handoff: RecordingStopHandoff | None = None,
) -> None:
    """Wait, then escalate to the process group. The drain is stderr_tail, joined only after this sequence, because joining it earlier filled stderr and stalled the child.

    ``terminate`` alone left a PortAudio thread holding the mic, and dropping
    the drain at the same time filled the stderr pipe and stalled the child.
    """
    proc = _recording_popen(managed)
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                log.warning("audio recorder pid=%s still alive after kill", proc.pid)
    # Closing stdout unblocks a read, and joining first is what keeps that
    # close off the common path; the fatal ValueError remains for a join that
    # times out or a reap with no handoff.
    if handoff is not None:
        handoff.request_monitor_stop()
    if monitor is not None and not monitor.is_current_thread():
        monitor.join(timeout=_RECORDING_MONITOR_JOIN_SEC)
    drain = managed.stderr_tail
    if drain is not None:
        drain.join(timeout=1.0)
    # Bugfix: the monitor closure holds proc, so the fds stayed open until
    # GC; closing the read end also unblocks a monitor stuck in os.read after
    # the child is gone. Do not close streams whose fileno is not an int.
    _close_recording_pipe(proc.stdin, "stdin")
    _close_recording_pipe(proc.stdout, "stdout")
    _close_recording_pipe(proc.stderr, "stderr")


def _known_path(handoff: RecordingStopHandoff | None, fallback_path: str | None) -> str | None:
    if handoff is not None:
        known = handoff.snapshot_path()
        if known:
            return known
    if isinstance(fallback_path, str) and fallback_path:
        return fallback_path
    return None


def _stop_recording_via_handoff(
    managed: ManagedSubprocess,
    handoff: RecordingStopHandoff,
    *,
    timeout_sec: float,
    fallback_path: str | None,
    monitor: BackgroundHandle | None = None,
) -> str:
    """Write stop and wait for the monitor. Do not read stdout."""
    # _stop_recording_via_handoff can return or raise without reaping the child
    # (wait_for_path timeout, stdin None, or an already-exited timeout).
    # try/finally _reap_recording_process reaps it and drops the stderr drain
    # on every exit.
    proc = _recording_popen(managed)
    try:
        if proc.poll() is not None:
            known = _known_path(handoff, fallback_path)
            if known:
                return known
            try:
                return handoff.wait_for_path(min(timeout_sec, 1.0))
            except RuntimeError:
                known = _known_path(handoff, fallback_path)
                if known:
                    return known
                raise RuntimeError("Recording subprocess already exited without a WAV path.") from None

        if proc.stdin is None:
            known = _known_path(handoff, fallback_path)
            if known:
                return known
            raise RuntimeError("Recording subprocess stdin is not available.")
        try:
            write_json_line(proc.stdin, {"command": "stop"})
        except OSError as exc:
            known = _known_path(handoff, fallback_path)
            if known:
                return known
            raise RuntimeError(f"Failed to signal recording subprocess: {exc}") from exc

        try:
            return handoff.wait_for_path(timeout_sec)
        except RuntimeError:
            known = _known_path(handoff, fallback_path)
            if known:
                return known
            raise
    finally:
        # Bugfix: this used to pass the 15s path timeout again, so Stop on the
        # VCL thread could block 15s plus another 15s before terminate. The
        # path wait above is the long timeout. After it returns or raises, 2s
        # plus the existing kill escalation is enough.
        _reap_recording_process(managed, _RECORDING_REAP_TIMEOUT_SEC, monitor=monitor, handoff=handoff)


def stop_recording_process(
    managed: ManagedSubprocess,
    *,
    handoff: RecordingStopHandoff,
    timeout_sec: float = _RECORDING_STOP_TIMEOUT_SEC,
    fallback_path: str | None = None,
    monitor: BackgroundHandle | None = None,
) -> str:
    """Send stop, then return the WAV path.

    The stdout monitor is the sole pipe reader. This function delegates
    to ``_stop_recording_via_handoff`` to signal stop and await the WAV path.
    This still does not read stdout.
    """
    return _stop_recording_via_handoff(
        managed,
        handoff,
        timeout_sec=timeout_sec,
        fallback_path=fallback_path,
        monitor=monitor,
    )


def _dispatch_recording_stdout(payload: dict[str, Any], *, handoff: RecordingStopHandoff | None, on_auto_stopped: Callable[[str], None], on_silence_progress: Callable[[int], None] | None, on_error: Callable[[str], None] | None) -> None:
    """Handle one child IPC line. Stash ``ok`` before any callback that may stop.

    Callbacks run on this thread. ``note_ok`` / ``note_error`` must happen
    first: a synchronous stop waits on the handoff, and it would deadlock if
    the path were published only after the callback returned.
    """
    status = payload.get("status")
    if status == "ok":
        path = payload.get("path")
        if isinstance(path, str) and path and handoff is not None:
            handoff.note_ok(path)
        return
    if status == "silence_progress" and on_silence_progress is not None:
        ms = payload.get("ms")
        if isinstance(ms, int):
            on_silence_progress(ms)
        return
    if status == "auto_stopped":
        path = payload.get("path")
        if isinstance(path, str) and path:
            if handoff is not None:
                handoff.note_ok(path)
            on_auto_stopped(path)
        return
    if status == "error":
        raw_msg = payload.get("message")
        message = raw_msg if isinstance(raw_msg, str) and raw_msg else "Audio recording failed."
        # Bugfix: with no callback, note_error never ran, so wait_for_path sat
        # for the full stop timeout. AudioRecorder always passes a callback;
        # other callers do not.
        if handoff is not None:
            handoff.note_error(message)
        if on_error is not None:
            on_error(message)
        return


def _stdout_monitor_value_error_is_fatal(exc: ValueError) -> bool:
    msg = str(exc)
    return msg.startswith((
        "I/O operation on closed file",
        "JSON line exceeds ",
        "JSON line truncated at EOF",
    ))


def monitor_recording_stdout(managed: ManagedSubprocess, *, on_auto_stopped: Callable[[str], None], on_silence_progress: Callable[[int], None] | None = None, on_error: Callable[[str], None] | None = None, handoff: RecordingStopHandoff | None = None) -> BackgroundHandle:
    """Sole stdout reader for venv recorder IPC.

    Pass the same *handoff* to ``stop_recording_process``. This thread consumes
    ``silence_progress``, ``auto_stopped``, ``error``, and the final ``ok``
    line. Stop must not read the pipe or it races this loop and loses the path.
    """

    def _reader() -> None:
        proc = cast("subprocess.Popen[str] | None", managed.popen)
        if proc is None or proc.stdout is None:
            return
        # Poll-first used to exit when the child wrote ``ok`` and exited in the
        # same moment, leaving that line unread. Read until EOF, and treat a
        # read timeout as done only after the process has exited.
        while True:
            if handoff is not None and handoff.monitor_stop_requested():
                break
            try:
                payload = read_json_line(proc.stdout, timeout_sec=0.25)
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    break
                continue
            except ValueError as exc:
                # Bugfix:
                # - What was wrong: after reap closed proc.stdout, the next
                #   read_json_line saw a closed text stream. _stream_fileno
                #   returns None on that ValueError, then readline() raises
                #   ValueError: I/O operation on closed file. The handler
                #   treated every ValueError as a bad line and continued, so
                #   the monitor spun and logged warnings. The same continue
                #   spins on "JSON line exceeds …", because _line_from_pending
                #   saves the oversize buffer and raises it again.
                # - How: reap closes stdout after the path wait, and the
                #   monitor is often between select calls (including while
                #   on_auto_stopped runs). select on a closed fd is OSError
                #   and already breaks; the gap is the closed-file ValueError.
                # - Why this fixes it: those three messages end the loop. A
                #   real non-JSON line still continues. Reap also sets the
                #   handoff event and joins this thread before closing, so the
                #   usual stop path does not close the pipe under the read.
                if _stdout_monitor_value_error_is_fatal(exc):
                    log.debug("Recording IPC monitor stopped: %s", exc)
                    break
                log.warning("Skipping non-JSON line from recording subprocess: %s", exc)
                continue
            except (RuntimeError, OSError) as exc:
                log.debug("Recording IPC monitor stopped: %s", exc)
                break
            if payload is None:
                # EOF after the child exited without an ok or error frame (a
                # crash): report it and wake a Stop waiting on the handoff
                # instead of leaving it to time out.
                if proc.poll() is not None and handoff is not None and not handoff.snapshot_path() and not handoff.has_error():
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

    return run_in_background(_reader, name="audio-rec-stdout-monitor", daemon=True, dedicated=True)


def terminate_recording_process(
    managed: ManagedSubprocess | None,
    *,
    monitor: BackgroundHandle | None = None,
    handoff: RecordingStopHandoff | None = None,
) -> None:
    """Best-effort shutdown of a recording child."""
    if managed is None:
        return
    proc = cast("subprocess.Popen[str] | None", managed.popen)
    if proc is None:
        return
    if proc.poll() is None:
        try:
            if proc.stdin is not None:
                write_json_line(proc.stdin, {"command": "stop"})
        except OSError:
            pass
    _reap_recording_process(managed, _RECORDING_REAP_TIMEOUT_SEC, monitor=monitor, handoff=handoff)


def make_temp_wav_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def check_host_audio_supported() -> bool:
    """Check if host-side audio recording is supported by trying to import sounddevice."""
    ensure_native_binaries_on_path()
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        return any(d.get("max_input_channels", 0) > 0 for d in devices)
    except Exception:
        return False


def is_audio_recording_supported(ctx: Any) -> bool:
    """True when either the user VENV is configured, or host-side audio libraries are installed."""
    if is_audio_recording_configured(ctx):
        return True
    return check_host_audio_supported()


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
            # On Windows the recorder subprocess still holds the WAV,
            # so os.remove raises PermissionError.
            log.debug("clear_pending_audio_wav: failed to remove %s: %s", host.audio_wav_path, exc)
        host.audio_wav_path = None
