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
from plugin.scripting.native_binaries import (
    _CONTRIB_BASE_URL,
    _download_url_to_file,
    ensure_downloaded_audio_on_path,
    run_vec_pack_download,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
from plugin.scripting.ipc import read_json_line, write_json_line
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_AUDIO_RECORD_MAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "venv", "audio_record_main.py"
)
_RECORDING_READY_TIMEOUT_SEC = 30
_RECORDING_STOP_TIMEOUT_SEC = 15
# Live stderr drains keyed by id(proc) — avoids pipe deadlock during record.
_recording_stderr_drains: dict[int, StderrTail] = {}

_VENV_NOT_CONFIGURED = (
    "Set the Python venv path in WriterAgent Settings → Python, then run "
    "'uv pip install sounddevice' in that venv."
)


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
        return (
            None,
            f"No python executable found under configured venv: {venv_dir!r} "
            "(expected bin/python, Scripts/python.exe, or env-root python.exe).",
        )
    return exe, ""


def _build_recording_env() -> dict[str, str]:
    env = scrub_subprocess_env(dict(os.environ))
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def _popen_kwargs() -> dict[str, Any]:
    popen_kw: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": _build_recording_env(),
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kw["preexec_fn"] = os.setsid
    return popen_kw


def _silence_cli_args(config: SilenceDetectorConfig) -> list[str]:
    return [f"--silence-stop-ms={max(0, config.silence_stop_ms)}"]


def spawn_recording_process(
    exe: str,
    output_path: str,
    *,
    silence_config: SilenceDetectorConfig | None = None,
) -> subprocess.Popen[str]:
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
    # Bugfix: this used to call stdout.readline() directly, so the ready/stop
    # timeout was ignored when the child hung before emitting JSON. The shared
    # IPC helper waits with a real deadline before reading the line.
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
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
    _recording_stderr_drains.pop(id(proc), None)


def stop_recording_process(
    proc: subprocess.Popen[str],
    *,
    timeout_sec: float = _RECORDING_STOP_TIMEOUT_SEC,
    fallback_path: str | None = None,
    handoff: RecordingStopHandoff | None = None,
) -> str:
    """Send stop, then return the WAV path.

    When *handoff* is set the stdout monitor is the only reader (see
    ``monitor_recording_stdout``). This function writes ``{"command":"stop"}``
    and waits on that handoff. It must not also call ``read_json_line``: that
    second reader stole ``ok`` and manual Stop Rec never got a path.
    """
    if handoff is not None:
        return _stop_recording_via_handoff(
            proc,
            handoff,
            timeout_sec=timeout_sec,
            fallback_path=fallback_path,
        )

    if proc.poll() is not None:
        if proc.stdout is not None:
            try:
                payload = read_json_line(proc.stdout, timeout_sec=0.25)
            except (subprocess.TimeoutExpired, ValueError, RuntimeError):
                payload = None
            if isinstance(payload, dict) and payload.get("status") == "ok":
                path = payload.get("path")
                if isinstance(path, str) and path:
                    return path
        if fallback_path:
            return fallback_path
        raise RuntimeError("Recording subprocess already exited without a WAV path.")

    if proc.stdin is None:
        raise RuntimeError("Recording subprocess stdin is not available.")
    try:
        write_json_line(proc.stdin, {"command": "stop"})
    except OSError as exc:
        raise RuntimeError(f"Failed to signal recording subprocess: {exc}") from exc

    payload = _read_json_line(proc, timeout_sec)
    status = payload.get("status")
    if status != "ok":
        message = payload.get("message") if status == "error" else f"Unexpected status {status!r}"
        raise RuntimeError(str(message or "Audio recording failed to stop."))
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise RuntimeError("Recording subprocess did not return a WAV path.")

    _reap_recording_process(proc, timeout_sec)
    return path


def _stop_recording_via_handoff(
    proc: subprocess.Popen[str],
    handoff: RecordingStopHandoff,
    *,
    timeout_sec: float,
    fallback_path: str | None,
) -> str:
    """Write stop and wait for the monitor. Do not read stdout."""

    def _fallback() -> str | None:
        if isinstance(fallback_path, str) and fallback_path:
            return fallback_path
        return None

    if proc.poll() is not None:
        # Child already exited (typical after silence auto-stop). The monitor
        # owns any ``ok`` still in the pipe; use the stashed path or the
        # auto-stop fallback instead of a competing read.
        known = handoff.snapshot_path() or _fallback()
        if known:
            _reap_recording_process(proc, timeout_sec)
            return known
        try:
            path = handoff.wait_for_path(min(timeout_sec, 1.0))
        except RuntimeError:
            known = handoff.snapshot_path() or _fallback()
            if known:
                return known
            raise RuntimeError("Recording subprocess already exited without a WAV path.") from None
        _reap_recording_process(proc, timeout_sec)
        return path

    if proc.stdin is None:
        known = handoff.snapshot_path() or _fallback()
        if known:
            return known
        raise RuntimeError("Recording subprocess stdin is not available.")
    try:
        write_json_line(proc.stdin, {"command": "stop"})
    except OSError as exc:
        # Auto-stop can close stdin between poll() and the write. The WAV path
        # is already on the handoff in that case.
        known = handoff.snapshot_path() or _fallback()
        if known:
            _reap_recording_process(proc, timeout_sec)
            return known
        raise RuntimeError(f"Failed to signal recording subprocess: {exc}") from exc

    try:
        path = handoff.wait_for_path(timeout_sec)
    except RuntimeError:
        known = handoff.snapshot_path()
        if known:
            _reap_recording_process(proc, timeout_sec)
            return known
        raise
    _reap_recording_process(proc, timeout_sec)
    return path


def _dispatch_recording_stdout(
    payload: dict[str, Any],
    *,
    handoff: RecordingStopHandoff | None,
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


def monitor_recording_stdout(
    proc: subprocess.Popen[str],
    *,
    on_auto_stopped: Callable[[str], None],
    on_silence_progress: Callable[[int], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    handoff: RecordingStopHandoff | None = None,
) -> BackgroundHandle:
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
            except (ValueError, RuntimeError) as exc:
                log.debug("Recording IPC monitor stopped: %s", exc)
                break
            if payload is None:
                break
            _dispatch_recording_stdout(
                payload,
                handoff=handoff,
                on_auto_stopped=on_auto_stopped,
                on_silence_progress=on_silence_progress,
                on_error=on_error,
            )

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
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    _recording_stderr_drains.pop(id(proc), None)


def make_temp_wav_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def check_host_audio_supported() -> bool:
    """Check if host-side audio recording is supported by trying to import sounddevice."""
    ensure_downloaded_audio_on_path()
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


def run_audio_download(on_display: Callable[[str], None], on_status: Callable[[str], None]) -> bool:
    """Download the pure-Python audio source zip and the platform-specific compiled binaries from GitHub."""
    import platform
    import sysconfig
    import zipfile

    from plugin.framework.config import user_config_dir

    ucd = user_config_dir()
    if not ucd:
        raise RuntimeError("User config directory not resolved.")

    target_dir = os.path.join(ucd, "audio_binaries")
    os.makedirs(target_dir, exist_ok=True)

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not ext_suffix:
        raise RuntimeError("Failed to determine Python EXT_SUFFIX.")

    cffi_name = f"_cffi_backend{ext_suffix}"

    portaudio_name = None
    if platform.system() == "Darwin":
        portaudio_name = "libportaudio.dylib"
    elif platform.system() == "Windows":
        is_arm = platform.machine().lower() in ("arm64", "aarch64")
        platform_suffix = "arm64" if is_arm else "64bit"
        portaudio_name = f"libportaudio{platform_suffix}.dll"

    base_url = _CONTRIB_BASE_URL

    on_display(f"Target directory: {target_dir}\n")
    on_display(f"Platform: {platform.system()} ({platform.machine()})\n")
    on_display(f"Python: {platform.python_version()}\n\n")

    # Download pure Python source zip
    zip_url = f"{base_url}audio_source.zip"
    zip_dest = os.path.join(target_dir, "audio_source.zip")
    on_display("Downloading pure Python audio libraries (audio_source.zip)...\n")
    _download_url_to_file(zip_url, zip_dest, on_status)

    # Extract audio_source.zip
    on_status("Extracting audio_source.zip...")
    on_display("Extracting audio_source.zip...\n")
    try:
        with zipfile.ZipFile(zip_dest, "r") as zf:
            zf.extractall(target_dir)
    except Exception as exc:
        raise RuntimeError(f"Failed to extract audio_source.zip: {exc}") from exc
    finally:
        if os.path.exists(zip_dest):
            try:
                os.remove(zip_dest)
            except Exception:
                pass

    # Download CFFI binary
    cffi_url = f"{base_url}audio/{cffi_name}"
    cffi_dest = os.path.join(target_dir, cffi_name)
    on_display(f"Downloading binary {cffi_name}...\n")
    _download_url_to_file(cffi_url, cffi_dest, on_status)

    # Download PortAudio binary if needed
    if portaudio_name:
        pa_url = f"{base_url}audio/_sounddevice_data/portaudio-binaries/{portaudio_name}"
        pa_dest = os.path.join(target_dir, "_sounddevice_data", "portaudio-binaries", portaudio_name)
        on_display(f"Downloading binary {portaudio_name}...\n")
        _download_url_to_file(pa_url, pa_dest, on_status)

    # Create _sounddevice_data/__init__.py placeholder
    init_dest = os.path.join(target_dir, "_sounddevice_data", "__init__.py")
    os.makedirs(os.path.dirname(init_dest), exist_ok=True)
    with open(init_dest, "w") as f:
        f.write("# Placeholder\n")

    run_vec_pack_download(on_display, on_status, include_header=False)
    on_display("\nAll downloaded files installed successfully!\n")
    return True


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
    True: the worker was respawned, or empty speech ended the turn.
    None: transcription threw. Stop the drain without a second API error;
    ``_transcribe_audio`` already reported it and deleted the WAV.
    """
    from plugin.audio.stt_service import uses_local_stt
    from plugin.framework.client.errors import is_audio_unsupported_error
    from plugin.framework.client.model_fetcher import get_stt_model, get_text_model, set_native_audio_support
    from plugin.framework.config import get_current_endpoint
    from plugin.framework.i18n import _

    if not host.audio_wav_path or not (_is_400_input_validation(error) or is_audio_unsupported_error(error)):
        return False

    current_model = get_text_model()
    current_endpoint = get_current_endpoint()
    log.warning("Model %s failed native audio, caching and falling back to STT" % current_model)
    set_native_audio_support(current_model, current_endpoint, supported=False)

    stt_model = get_stt_model()
    # Local Whisper does not need an endpoint model id. Endpoint STT still does.
    local_stt = uses_local_stt()
    retry_q = host._active_batched_q or host._active_q
    if (stt_model or local_stt) and retry_q is not None and host._active_client is not None:
        host._append_response("\n[Model does not support audio. Falling back to STT...]\n")
        try:
            transcript = host._transcribe_audio(host.audio_wav_path, stt_model)
            wav_path = host.audio_wav_path
            host.audio_wav_path = None
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError as rem_err:
                    log.debug("Failed to remove audio_wav_path after STT fallback: %s", rem_err)
            if not (transcript or "").strip():
                # G27: empty STT must not spawn a blank chat POST.
                host._append_response("\n" + _("[No speech detected.]") + "\n")
                host._terminal_status = ""
                return True
            combined = (host._active_query_text + "\n" + transcript).strip() if host._active_query_text else transcript
            if host.session.messages and host.session.messages[-1].get("role") == "user":
                host.session.messages.pop()
            host.session.add_user_message(combined)
            host._active_query_text = combined
            host._spawn_llm_worker(
                retry_q,
                host._active_client,
                host._active_max_tokens,
                host._active_tools or [],
                host._sm_state.round_num,
                query_text=combined,
            )
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
