# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side helpers for venv microphone recording subprocess."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable

from plugin.framework.config import get_config_str
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
from plugin.scripting.ipc import read_json_line, write_json_line
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_AUDIO_RECORD_MAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "venv", "audio_record_main.py"
)
_RECORDING_READY_TIMEOUT_SEC = 30
_RECORDING_STOP_TIMEOUT_SEC = 15

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
            "(expected bin/python, bin/python3, or bin/python3.x).",
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
    return subprocess.Popen(cmd, **_popen_kwargs())


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
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
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


def stop_recording_process(
    proc: subprocess.Popen[str],
    *,
    timeout_sec: float = _RECORDING_STOP_TIMEOUT_SEC,
    fallback_path: str | None = None,
) -> str:
    """Send stop, read final JSON line, terminate child, return WAV path."""
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

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
    return path


def monitor_recording_stdout(
    proc: subprocess.Popen[str],
    *,
    on_auto_stopped: Callable[[str], None],
    on_silence_progress: Callable[[int], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> threading.Thread:
    """Background reader for venv recorder IPC (auto-stop and silence progress)."""

    def _reader() -> None:
        if proc.stdout is None:
            return
        while proc.poll() is None:
            try:
                payload = read_json_line(proc.stdout, timeout_sec=0.25)
            except subprocess.TimeoutExpired:
                continue
            except (ValueError, RuntimeError) as exc:
                log.debug("Recording IPC monitor stopped: %s", exc)
                break
            if payload is None:
                break
            status = payload.get("status")
            if status == "silence_progress" and on_silence_progress is not None:
                ms = payload.get("ms")
                if isinstance(ms, int):
                    on_silence_progress(ms)
            elif status == "auto_stopped":
                path = payload.get("path")
                if isinstance(path, str) and path:
                    on_auto_stopped(path)
            elif status == "error" and on_error is not None:
                message = payload.get("message")
                if isinstance(message, str):
                    on_error(message)

    return run_in_background(_reader, name="audio-rec-stdout-monitor", daemon=True)


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


def make_temp_wav_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def ensure_downloaded_audio_on_path() -> None:
    """Ensure downloaded pure Python audio files and platform binaries are on sys.path."""
    from plugin.framework.config import user_config_dir
    try:
        ucd = user_config_dir()
        if ucd:
            bin_dir = os.path.join(ucd, "audio_binaries")
            if os.path.isdir(bin_dir) and bin_dir not in sys.path:
                sys.path.insert(0, bin_dir)
    except Exception as exc:
        log.debug("Failed to add user config audio path to sys.path: %s", exc)


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
    import sysconfig
    import platform
    import urllib.request
    import urllib.error
    import zipfile
    from plugin.framework.config import user_config_dir

    ucd = user_config_dir()
    if not ucd:
        raise RuntimeError("User config directory not resolved.")

    target_dir = os.path.join(ucd, "audio_binaries")
    os.makedirs(target_dir, exist_ok=True)

    ext_suffix = sysconfig.get_config_var('EXT_SUFFIX')
    if not ext_suffix:
        raise RuntimeError("Failed to determine Python EXT_SUFFIX.")

    cffi_name = f"_cffi_backend{ext_suffix}"

    portaudio_name = None
    if platform.system() == 'Darwin':
        portaudio_name = 'libportaudio.dylib'
    elif platform.system() == 'Windows':
        is_arm = platform.machine().lower() in ('arm64', 'aarch64')
        platform_suffix = 'arm64' if is_arm else '64bit'
        portaudio_name = f'libportaudio{platform_suffix}.dll'

    base_url = "https://raw.githubusercontent.com/KeithCu/writeragent/master/contrib/"

    on_display(f"Target directory: {target_dir}\n")
    on_display(f"Platform: {platform.system()} ({platform.machine()})\n")
    on_display(f"Python: {platform.python_version()}\n\n")

    def download_url_to_file(url: str, dest_path: str) -> None:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        try:
            with urllib.request.urlopen(req) as response:
                total_size = int(response.headers.get('content-length', 0))
                block_size = 8192
                downloaded = 0
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, 'wb') as f:
                    while True:
                        buffer = response.read(block_size)
                        if not buffer:
                            break
                        downloaded += len(buffer)
                        f.write(buffer)
                        if total_size:
                            percent = int(downloaded * 100 / total_size)
                            on_status(f"Downloading {os.path.basename(dest_path)}: {percent}%")
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"HTTP Error {err.code}: {err.reason} for URL: {url}") from err
        except Exception as exc:
            raise RuntimeError(f"Failed to download {url}: {exc}") from exc

    # Download pure Python source zip
    zip_url = f"{base_url}audio_source.zip"
    zip_dest = os.path.join(target_dir, "audio_source.zip")
    on_display("Downloading pure Python audio libraries (audio_source.zip)...\n")
    download_url_to_file(zip_url, zip_dest)

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
    download_url_to_file(cffi_url, cffi_dest)

    # Download PortAudio binary if needed
    if portaudio_name:
        pa_url = f"{base_url}audio/_sounddevice_data/portaudio-binaries/{portaudio_name}"
        pa_dest = os.path.join(target_dir, "_sounddevice_data", "portaudio-binaries", portaudio_name)
        on_display(f"Downloading binary {portaudio_name}...\n")
        download_url_to_file(pa_url, pa_dest)

    # Create _sounddevice_data/__init__.py placeholder
    init_dest = os.path.join(target_dir, "_sounddevice_data", "__init__.py")
    os.makedirs(os.path.dirname(init_dest), exist_ok=True)
    with open(init_dest, "w") as f:
        f.write("# Placeholder\n")

    # Download writeragent_vec serialization binaries
    vec_init_url = f"{base_url}vec_pack/__init__.py"
    vec_init_dest = os.path.join(target_dir, "writeragent_vec", "__init__.py")
    on_display("Downloading writeragent_vec/__init__.py...\n")
    download_url_to_file(vec_init_url, vec_init_dest)

    pack_name = f"pack{ext_suffix}"
    vec_bin_url = f"{base_url}vec_pack/{pack_name}"
    vec_bin_dest = os.path.join(target_dir, "writeragent_vec", pack_name)
    on_display(f"Downloading binary {pack_name}...\n")
    download_url_to_file(vec_bin_url, vec_bin_dest)

    on_display("\nAll downloaded files installed successfully!\n")
    return True
