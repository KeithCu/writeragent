# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side helpers for venv microphone recording and chat audio.

Recording spawn/IPC stays in this module. Chat also uses it to attach a
finished WAV as ``input_audio`` and to recover when that native-audio POST
is rejected, so the tool loop does not own those steps.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any

from plugin.framework.config import get_config_str
from plugin.framework.worker_pool import BackgroundHandle, StderrTail, run_in_background, start_stderr_drain
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
# Live stderr drains keyed by id(proc) — avoids pipe deadlock during record.
_recording_stderr_drains: dict[int, StderrTail] = {}

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


def _popen_kwargs() -> dict[str, Any]:
    popen_kw: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": _build_recording_env(), "text": True, "bufsize": 1}
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kw["start_new_session"] = True
    return popen_kw


def _silence_cli_args(config: SilenceDetectorConfig) -> list[str]:
    return [f"--silence-stop-ms={max(0, config.silence_stop_ms)}"]


def spawn_recording_process(exe: str, output_path: str, *, silence_config: SilenceDetectorConfig | None = None) -> subprocess.Popen[str]:
    """Start audio_record_main.py in the user venv."""
    cmd = [exe, _AUDIO_RECORD_MAIN, "--output", output_path]
    if silence_config is not None:
        cmd.extend(_silence_cli_args(silence_config))
    cmd = wrap_command_for_sandbox(cmd)
    proc = subprocess.Popen(cmd, **_popen_kwargs())
    drain = start_stderr_drain(proc.stderr, name=f"audio-rec-stderr-{proc.pid}")
    if drain is not None:
        _recording_stderr_drains[id(proc)] = drain
    return proc


class RecordingStopHandoff:
    """WAV path stashed by the sole stdout monitor for ``stop_recording_process``.

    Manual Stop Rec used to call ``read_json_line`` on the same pipe the silence
    monitor was already reading. The monitor only handled ``silence_progress``,
    ``auto_stopped``, and ``error``, so it dropped ``{"status":"ok","path":…}``.
    Stop then timed out, and the panel deleted the temp WAV and sent nothing.
    """

    _lock: threading.Lock
    _ready: threading.Event
    _path: str | None
    _error: str | None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._path = None
        self._error = None

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


def _read_json_line(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
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
        drain = _recording_stderr_drains.get(id(proc))
        stderr = (drain.finish_text() if drain is not None else "") or ""
        code = proc.poll()
        detail = stderr.strip() or f"exit code {code}"
        raise RuntimeError(f"Recording subprocess ended before responding ({detail}).")
    return payload


def wait_for_recording_ready(proc: subprocess.Popen[str], *, timeout_sec: float = _RECORDING_READY_TIMEOUT_SEC) -> None:
    """Block until the child emits ``{\"status\": \"ready\"}`` or fails."""
    payload = _read_json_line(proc, timeout_sec)
    status = payload.get("status")
    if status == "ready":
        return
    if status == "error":
        raise RuntimeError(str(payload.get("message") or "Audio recording failed to start."))
    raise RuntimeError(f"Unexpected recording subprocess status: {status!r}")


def _reap_recording_process(proc: subprocess.Popen[str], timeout_sec: float) -> None:
    """Wait, then escalate to the process group. Drop the stderr drain only after exit.

    ``terminate`` alone left a PortAudio thread holding the mic, and dropping
    the drain at the same time filled the stderr pipe and stalled the child.
    """
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            from plugin.scripting.venv_worker import _kill_process_tree

            _kill_process_tree(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                log.warning("audio recorder pid=%s still alive after kill", proc.pid)
    drain = _recording_stderr_drains.pop(id(proc), None)
    if drain is not None:
        drain.join(timeout=1.0)


def _known_path(handoff: RecordingStopHandoff | None, fallback_path: str | None) -> str | None:
    if handoff is not None:
        known = handoff.snapshot_path()
        if known:
            return known
    if isinstance(fallback_path, str) and fallback_path:
        return fallback_path
    return None


def _stop_recording_via_handoff(
    proc: subprocess.Popen[str],
    handoff: RecordingStopHandoff,
    *,
    timeout_sec: float,
    fallback_path: str | None,
) -> str:
    """Write stop and wait for the monitor. Do not read stdout."""
    # _stop_recording_via_handoff can return or raise without reaping the child
    # (wait_for_path timeout, stdin None, or an already-exited timeout).
    # try/finally _reap_recording_process reaps it and drops the stderr drain
    # on every exit.
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
        _reap_recording_process(proc, timeout_sec)


def stop_recording_process(
    proc: subprocess.Popen[str],
    *,
    timeout_sec: float = _RECORDING_STOP_TIMEOUT_SEC,
    fallback_path: str | None = None,
    handoff: RecordingStopHandoff | None = None,
) -> str:
    """Send stop, then return the WAV path.

    The stdout monitor is the sole pipe reader. This function delegates
    to ``_stop_recording_via_handoff`` to signal stop and await the WAV path.
    """
    if handoff is None:
        handoff = RecordingStopHandoff()
    return _stop_recording_via_handoff(proc, handoff, timeout_sec=timeout_sec, fallback_path=fallback_path)


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
    if status == "error" and on_error is not None:
        message = payload.get("message")
        if isinstance(message, str):
            if handoff is not None:
                handoff.note_error(message)
            on_error(message)


def monitor_recording_stdout(proc: subprocess.Popen[str], *, on_auto_stopped: Callable[[str], None], on_silence_progress: Callable[[int], None] | None = None, on_error: Callable[[str], None] | None = None, handoff: RecordingStopHandoff | None = None) -> BackgroundHandle:
    """Sole stdout reader for venv recorder IPC.

    Pass the same *handoff* to ``stop_recording_process``. This thread consumes
    ``silence_progress``, ``auto_stopped``, ``error``, and the final ``ok``
    line. Stop must not read the pipe or it races this loop and loses the path.
    """

    def _reader() -> None:
        if proc.stdout is None:
            return
        # Poll-first used to exit when the child wrote ``ok`` and exited in the
        # same moment, leaving that line unread. Read until EOF, and treat a
        # read timeout as done only after the process has exited.
        while True:
            try:
                payload = read_json_line(proc.stdout, timeout_sec=0.25)
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    break
                continue
            except ValueError as exc:
                # Non-JSON lines (ALSA/PortAudio warnings, stray prints) raise
                # ValueError. Skip and log them so the monitor stays up until 'ok'.
                log.warning("Skipping non-JSON line from recording subprocess: %s", exc)
                continue
            except RuntimeError as exc:
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
                        on_error(message)
                break
            _dispatch_recording_stdout(payload, handoff=handoff, on_auto_stopped=on_auto_stopped, on_silence_progress=on_silence_progress, on_error=on_error)

    return run_in_background(_reader, name="audio-rec-stdout-monitor", daemon=True, dedicated=True)


def terminate_recording_process(proc: subprocess.Popen[str] | None) -> None:
    """Best-effort shutdown of a recording child."""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            if proc.stdin is not None:
                write_json_line(proc.stdin, {"command": "stop"})
        except OSError:
            pass
    _reap_recording_process(proc, 2.0)


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


def _is_400_input_validation(err: Any) -> bool:
    """Treat HTTP 400 with 'input validation' or 'bad request' as likely audio-format rejection (e.g. Together AI)."""
    msg = str(err).lower()
    return "400" in msg and ("input validation" in msg or "bad request" in msg)


def append_wav_as_input_audio(content_list: list[dict[str, Any]], wav_path: str) -> bool:
    """Read a WAV and append an OpenAI ``input_audio`` content part.

    Returns False when the file cannot be read. The caller clears
    ``audio_wav_path`` and still posts the text or image message.
    """
    from plugin.framework.errors import NetworkError

    try:
        with open(wav_path, "rb") as f:
            wav_data = f.read()
        b64_audio = base64.b64encode(wav_data).decode("utf-8")
        content_list.append({"type": "input_audio", "input_audio": {"data": b64_audio, "format": "wav"}})
        return True
    except (IOError, OSError):
        log.exception("Audio file error")
        log.debug("Audio file preserved at: %s" % wav_path)
        return False
    except Exception as e:
        if isinstance(e, NetworkError):
            log.exception("NetworkError while handling audio message")
        else:
            log.exception("Unexpected audio error")
        return False


def try_native_audio_stt_fallback(host: Any, error: Any) -> bool | None:
    """After a native ``input_audio`` rejection, transcribe and respawn this drain.

    The chat POST already failed, so this must not re-enter
    ``_do_send_chat_with_tools`` / ``_start_tool_calling_async`` (nested drain).
    Model-catalog imports stay inside this function so LibrePy can load the
    recorder helpers without pulling the chat model fetcher.

    False: not a native-audio rejection, or STT cannot run. The caller
    continues with overflow and generic API handling.
    True: a replacement worker was spawned on this drain. The caller must
    keep draining. The drain treats only True that way.
    None: stop the drain without a second API error. Transcription threw
    (``_transcribe_audio`` already reported it and deleted the WAV), or
    empty speech already ended the turn (banner shown, no worker).
    """
    from plugin.audio.stt_service import uses_local_stt
    from plugin.framework.client.errors import is_audio_unsupported_error
    from plugin.framework.client.model_fetcher import get_stt_model, set_native_audio_support
    from plugin.framework.i18n import _

    if not host.audio_wav_path or not (_is_400_input_validation(error) or is_audio_unsupported_error(error)):
        return False

    turn = getattr(host, "_turn", None)
    # Read text_model and endpoint from the turn captured when the audio was
    # attached. Re-reading the combobox mid-turn would mark the newly chosen
    # model unsupported instead of the model the audio was sent to.
    turn_model = getattr(turn, "text_model", None)
    turn_endpoint = getattr(turn, "endpoint", None)
    if not turn_model:
        from plugin.framework.client.model_fetcher import get_text_model
        from plugin.framework.config import get_current_endpoint

        turn_model = get_text_model()
        turn_endpoint = get_current_endpoint()
    if turn_model:
        log.warning("Model %s failed native audio, caching and falling back to STT", turn_model)
        set_native_audio_support(turn_model, turn_endpoint, supported=False)

    stt_model = get_stt_model()
    # Local Whisper does not need an endpoint model id. Endpoint STT still does.
    local_stt = uses_local_stt()
    retry_q = None
    if turn is not None and getattr(turn, "alive", False):
        retry_q = getattr(turn, "batcher", None) or getattr(turn, "queue", None)
    if (stt_model or local_stt) and retry_q is not None and host._active_client is not None:
        host._append_response("\n[Model does not support audio. Falling back to STT...]\n")
        try:
            transcript = host._transcribe_audio(host.audio_wav_path, stt_model)
            if getattr(host, "_terminal_status", None) == "Stopped":
                # Stop during fallback STT. None ends the drain. Do not show
                # "No speech detected" or spawn another chat worker.
                return None
            wav_path = host.audio_wav_path
            host.audio_wav_path = None
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError as rem_err:
                    log.debug("Failed to remove audio_wav_path after STT fallback: %s", rem_err)
            if not (transcript or "").strip():
                # G27: empty STT must not spawn a blank chat POST.
                # The drain keeps running only when on_error returns True,
                # which means a replacement worker was spawned. Empty speech
                # spawns nothing and posts no STREAM_DONE, so returning True
                # left the sidebar on Stop. None means this turn is finished
                # and a second API error should not be shown.
                host._append_response("\n" + _("[No speech detected.]") + "\n")
                host._terminal_status = ""
                return None
            combined = (host._active_query_text + "\n" + transcript).strip() if host._active_query_text else transcript
            if host.session.messages and host.session.messages[-1].get("role") == "user":
                host.session.messages[-1]["content"] = combined
                if getattr(host.session, "db", None):
                    rows = host.session.db.get_messages()
                    # Find the last user row and replace it
                    for row in reversed(rows):
                        if row.get("role") == "user":
                            row["content"] = combined
                            break
                    else:
                        rows.append({"role": "user", "content": combined})
                    host.session.db.replace_messages(rows)
            else:
                host.session.add_user_message(combined)
            host._active_query_text = combined
            host._spawn_llm_worker(retry_q, host._active_client, host._active_max_tokens, host._active_tools or [], host._sm_state.round_num, query_text=combined)
            return True
        except Exception:
            log.exception("STT fallback after native-audio error failed")
        return None
    return False


def clear_pending_audio_wav(host: Any) -> None:
    """Delete the pending recording after a chat error that did not fall back to STT."""
    if host.audio_wav_path:
        try:
            os.remove(host.audio_wav_path)
        except OSError as rem_err:
            log.debug("Failed to remove audio_wav_path during error handling: %s", rem_err)
        host.audio_wav_path = None
