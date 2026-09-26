# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Speech-to-text provider dispatch.

``endpoint`` (the default) keeps ``LlmClient.transcribe_audio``
(POST ``/v1/audio/transcriptions``, or chat when that STT model takes audio).

``local`` (aliases ``whisper`` and ``faster-whisper``) runs
``whisper_transcribe.py`` in the Settings → Python venv. The package is
user-installed (Settings → Python Test lists it with the other Audio
optional packages). Record does not pip-install it. Model weights still
download into the Hugging Face cache the first time a size is used — the
same binary-fetch pattern as Kokoro and Piper voice files.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Callable

from plugin.framework.config import get_config, get_config_str
from plugin.framework.errors import ConfigError
from plugin.framework.i18n import _
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

# Shown when the venv cannot ``import faster_whisper``. Record does not run it.
FASTER_WHISPER_PIP_INSTALL = "uv pip install faster-whisper"

STT_PROVIDER_ENDPOINT = "endpoint"
STT_PROVIDER_LOCAL = "local"
# Stored value is ``local``. These are accepted from writeragent.json and the
# Speech combo label ("Local Whisper (faster-whisper)").
_LOCAL_PROVIDER_ALIASES = frozenset({"local", "whisper", "faster-whisper", "faster_whisper"})

# faster-whisper size aliases. Each downloads Systran/faster-whisper-<size>
# into the Hugging Face cache the first time that size is used.
LOCAL_STT_MODELS = ("tiny", "base", "small", "medium")
DEFAULT_STT_LOCAL_MODEL = "base"

_PROBE_TIMEOUT_SEC = 60.0
# First run may download weights (base is ~150 MB; medium is ~1.5 GB) and then
# transcribe. A short recording on CPU is much less than this.
_TRANSCRIBE_TIMEOUT_SEC = 900.0

_WHISPER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper_transcribe.py")

_probe_lock = threading.Lock()
_whisper_ready: set[str] = set()


def normalize_stt_provider(raw: object) -> str:
    """Map settings text and legacy aliases to ``endpoint`` or ``local``.

    Missing and unknown values stay ``endpoint`` so an old config (no
    ``audio.stt_provider``) keeps POST ``/v1/audio/transcriptions``.
    """
    text = str(raw or "").strip().lower()
    if text in _LOCAL_PROVIDER_ALIASES or "whisper" in text:
        return STT_PROVIDER_LOCAL
    return STT_PROVIDER_ENDPOINT


def normalize_stt_local_model(raw: object) -> str:
    """Map a size label or id to a faster-whisper model name.

    ``base (~150 MB)`` and ``SMALL`` become ``base`` / ``small``. A Hugging
    Face id with no spaces (for example ``Systran/faster-whisper-base``) is
    kept so a hand-edited config is not rewritten to the default.
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_STT_LOCAL_MODEL
    low = text.lower()
    for size in LOCAL_STT_MODELS:
        if low == size or low.startswith(size + " ") or low.startswith(size + "("):
            return size
    if " " not in text and len(text) <= 200 and not text.startswith("{"):
        return text
    return DEFAULT_STT_LOCAL_MODEL


def stt_controls_enabled(provider: str) -> tuple[bool, bool]:
    """Return ``(endpoint Audio Model enabled, local model enabled)``.

    Speech settings shows one of the two, matching TTS Model (only for the
    endpoint provider).
    """
    local = normalize_stt_provider(provider) == STT_PROVIDER_LOCAL
    return (not local, local)


def get_stt_provider() -> str:
    """``audio.stt_provider``, or ``endpoint`` when the key is absent."""
    try:
        raw = get_config("audio.stt_provider")
    except ConfigError:
        # Manifest without this key (checkout before the field existed).
        raw = ""
    return normalize_stt_provider(raw)


def get_stt_local_model() -> str:
    """``audio.stt_local_model``, default ``base``."""
    try:
        raw = get_config("audio.stt_local_model")
    except ConfigError:
        raw = ""
    return normalize_stt_local_model(raw)


def uses_local_stt() -> bool:
    """True when Record should transcribe in the venv instead of over HTTP."""
    return get_stt_provider() == STT_PROVIDER_LOCAL


def status_for_transcription() -> str:
    """Sidebar status before the blocking transcribe call."""
    if uses_local_stt():
        return _("Transcribing with local Whisper ({0})…").format(get_stt_local_model())
    return _("Transcribing audio...")


def clear_faster_whisper_probe_cache() -> None:
    """Forget which venv interpreters already imported faster-whisper."""
    with _probe_lock:
        _whisper_ready.clear()


def resolve_stt_python() -> str | None:
    """Venv interpreter from Settings → Python, or None when it is not set."""
    try:
        venv_dir = get_config_str("scripting.python_venv_path").strip()
    except ConfigError:
        return None
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def _emit(on_status: Callable[[str], None] | None, message: str) -> None:
    log.info("%s", message)
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception:
        log.exception("STT status callback failed")


def _run_cmd(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    # A console window on Windows would flash over the document during Record.
    # Same flag as plugin/scripting/audio_recorder_service.py _popen_kwargs.
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
        "stdin": subprocess.DEVNULL,
        "env": scrub_subprocess_env(dict(os.environ)),
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(wrap_command_for_sandbox(cmd), **run_kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("STT command failed (%s): %s", cmd[0], exc)
        return None


def _probe_faster_whisper(py_exe: str) -> bool:
    completed = _run_cmd([py_exe, "-c", "import faster_whisper"], _PROBE_TIMEOUT_SEC)
    return completed is not None and completed.returncode == 0


def ensure_faster_whisper(py_exe: str) -> bool:
    """Return True when ``import faster_whisper`` succeeds in ``py_exe``.

    A successful probe is remembered for this process so the next recording
    does not spawn Python again just to import. A missing package is not
    installed and is not cached: Record raises ``ConfigError`` with the
    install hint. Auto-pip on first Record failed on real machines; Kokoro
    and Piper auto-fetch voice files, not their Python packages.
    """
    with _probe_lock:
        if py_exe in _whisper_ready:
            return True
    if _probe_faster_whisper(py_exe):
        with _probe_lock:
            _whisper_ready.add(py_exe)
        return True
    return False


def parse_whisper_stdout(stdout: str) -> str:
    """Return the transcript from the child's JSON lines.

    Non-JSON lines (download logs, if any) are ignored. The last ``error``
    object wins when there is no ``ok``.
    """
    last_error = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        if status == "ok":
            text = payload.get("text")
            return text if isinstance(text, str) else ""
        if status == "error":
            message = payload.get("message")
            last_error = message if isinstance(message, str) and message.strip() else "Local Whisper failed"
    if last_error:
        raise ConfigError(_("Local Whisper failed: {0}").format(last_error))
    raise ConfigError(_("Local Whisper did not return a transcript."))


def _missing_venv_message() -> str:
    return _(
        "Local Whisper runs in the Settings → Python venv, not LibreOffice's Python. "
        "Set the venv, then install with: {0}"
    ).format(FASTER_WHISPER_PIP_INSTALL)


def _missing_package_message() -> str:
    return _(
        "faster-whisper is not installed in the Settings → Python venv. "
        "Install with: {0}. Settings → Python Test lists it under Audio optional packages."
    ).format(FASTER_WHISPER_PIP_INSTALL)


def _transcribe_local(
    wav_path: str,
    model_name: str,
    on_status: Callable[[str], None] | None,
) -> str:
    py_exe = resolve_stt_python()
    if not py_exe:
        raise ConfigError(_missing_venv_message())
    if not os.path.isfile(_WHISPER_SCRIPT):
        raise ConfigError(_("Local Whisper script is missing from the extension."))
    if not ensure_faster_whisper(py_exe):
        raise ConfigError(_missing_package_message())

    # WhisperModel downloads on first use inside the child. The sidebar status
    # is set here because that download does not stream progress back.
    _emit(
        on_status,
        _("Transcribing with local Whisper ({0})… The first run downloads the model.").format(model_name),
    )
    completed = _run_cmd(
        [py_exe, _WHISPER_SCRIPT, "--wav", wav_path, "--model", model_name],
        _TRANSCRIBE_TIMEOUT_SEC,
    )
    if completed is None:
        raise ConfigError(
            _(
                "Local Whisper timed out or could not start. The first run downloads model weights; "
                "try again, or pick a smaller Local Model in Settings → Speech."
            )
        )
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        detail = (completed.stderr or completed.stdout or "").strip()
        log.warning("Local Whisper exited %s: %s", completed.returncode, detail[-2000:])
    try:
        return parse_whisper_stdout(completed.stdout or "")
    except ConfigError:
        detail = (completed.stderr or "").strip()
        if detail and completed.returncode != 0:
            log.warning("Local Whisper stderr: %s", detail[-2000:])
        raise


def transcribe(
    wav_path: str,
    *,
    client: Any = None,
    model: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """Transcribe ``wav_path`` with the configured STT provider.

    ``model`` is the endpoint STT id (``audio.stt_model``). Local Whisper
    ignores it and uses ``audio.stt_local_model``. Endpoint calls
    ``client.transcribe_audio`` and does not spawn the venv.
    """
    if uses_local_stt():
        return _transcribe_local(wav_path, get_stt_local_model(), on_status)
    if client is None or not hasattr(client, "transcribe_audio"):
        raise ConfigError(_("No language-model client is available for endpoint speech-to-text."))
    return str(client.transcribe_audio(wav_path, model=model) or "")
