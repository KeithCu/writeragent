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
same binary-fetch pattern as Kokoro and Piper voice files. The status line
mentions that download only when those weights are not already cached.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from plugin.framework.config import get_config, get_config_str
from plugin.framework.errors import ConfigError
from plugin.framework.i18n import _
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

# Shown when the venv cannot ``import faster_whisper``. Record does not run it.
FASTER_WHISPER_PIP_INSTALL = 'uv pip install faster-whisper "av<14"'

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
# transcribe. A short recording on CPU is much less than this. Stop kills the
# child; this is only the bound when the user does not press Stop.
_TRANSCRIBE_TIMEOUT_SEC = 900.0
# How often the host re-checks Stop while the child is alive. The cancel hook
# kills the process immediately; this wait is the backup poll.
_STT_POLL_SEC = 0.2

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


def _hf_hub_cache_dir() -> str:
    """Hub cache ``snapshot_download`` uses when ``cache_dir`` is omitted.

    Same order as ``huggingface_hub.constants``: ``HF_HUB_CACHE``, else
    ``HUGGINGFACE_HUB_CACHE``, else ``$HF_HOME/hub``, else
    ``$XDG_CACHE_HOME/huggingface/hub``, else ``~/.cache/huggingface/hub``.
    faster-whisper ``download_model`` does not pass ``cache_dir``
    (``faster_whisper/utils.py``).
    """
    default_home = os.path.join(os.path.expanduser("~"), ".cache")
    hf_home = os.path.expandvars(
        os.path.expanduser(
            os.environ.get("HF_HOME")
            or os.path.join(os.environ.get("XDG_CACHE_HOME") or default_home, "huggingface")
        )
    )
    default_cache = os.path.join(hf_home, "hub")
    legacy = os.environ.get("HUGGINGFACE_HUB_CACHE") or default_cache
    return os.path.expandvars(os.path.expanduser(os.environ.get("HF_HUB_CACHE") or legacy))


def _faster_whisper_repo_id(model_name: str) -> str | None:
    """Repo id ``download_model`` would fetch, or None when it would not.

    A value containing ``/`` is the repo id (``faster_whisper/utils.py``
    ``re.match(r".*/.*")``). ``tiny`` / ``base`` / ``small`` / ``medium`` are
    the ``_MODELS`` aliases ``Systran/faster-whisper-<size>``. Anything else
    makes ``download_model`` raise rather than fetch.
    """
    if "/" in model_name:
        return model_name
    if model_name in LOCAL_STT_MODELS:
        return "Systran/faster-whisper-" + model_name
    return None


def _snapshot_has_model_bin(snapshot_dir: str) -> bool:
    """True when this snapshot's ``model.bin`` is a non-empty file.

    ``model.bin`` is the weight file in ``download_model``'s ``allow_patterns``.
    Hub snapshots symlink it at ``blobs/``; ``isfile`` follows that link and
    rejects a dangling one left by an interrupted download.
    """
    weights = os.path.join(snapshot_dir, "model.bin")
    try:
        return os.path.isfile(weights) and os.path.getsize(weights) > 0
    except OSError:
        return False


def _weights_cached_in_hub(repo_id: str) -> bool:
    """True when ``repo_id`` already has a snapshot ``model.bin`` in the hub cache."""
    # huggingface_hub folder: models--org--name (slash in the repo id becomes --).
    folder = "models--" + repo_id.replace("/", "--")
    snapshots = os.path.join(_hf_hub_cache_dir(), folder, "snapshots")
    try:
        entries = os.listdir(snapshots)
    except OSError:
        return False
    for entry in entries:
        if _snapshot_has_model_bin(os.path.join(snapshots, entry)):
            return True
    return False


def _local_whisper_weights_cached(model_name: str) -> bool:
    """True when this transcription will not download weights.

    ``WhisperModel`` loads an existing directory in place and does not call
    ``download_model`` (``faster_whisper/transcribe.py``). A size or HF id is
    cached when its hub snapshot already contains ``model.bin``.
    """
    text = str(model_name or "").strip()
    if not text:
        return False
    if os.path.isdir(text):
        return True
    repo_id = _faster_whisper_repo_id(text)
    if repo_id is None:
        return True
    return _weights_cached_in_hub(repo_id)


def _local_whisper_status(model_name: str) -> str:
    """Sidebar line for local Whisper. Download wording only if weights are missing."""
    if _local_whisper_weights_cached(model_name):
        return _("Transcribing with local Whisper ({0})…").format(model_name)
    return _("Transcribing with local Whisper ({0})… The first run downloads the model.").format(model_name)


def status_for_transcription() -> str:
    """Sidebar status before the blocking transcribe call."""
    if uses_local_stt():
        return _local_whisper_status(get_stt_local_model())
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


class SttStopped(Exception):
    """Stop aborted speech-to-text before a transcript existed.

    Not a :class:`ConfigError`. The sidebar must end the turn as Stopped
    and must not paint a transcription failure for that click.
    """


def terminate_stt_process(proc: subprocess.Popen[str] | None) -> None:
    """Kill a Whisper or probe child. A second call is a no-op.

    ``SIGKILL`` (``kill``) so Stop does not wait on a 900s ``subprocess.run``.
    The caller reaps with ``wait``. Do not ``wait`` here: this runs on the UI
    thread from ``SendCancellation.cancel``.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        log.debug("STT kill failed", exc_info=True)


def _reap_stt_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=1.0)
    except Exception:
        log.debug("STT reap failed", exc_info=True)


def _arm_stt_cancel(
    proc: subprocess.Popen[str],
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> None:
    if on_spawn is not None:
        try:
            on_spawn(proc)
        except Exception:
            log.exception("STT on_spawn failed")


def _read_stt_pipe(stream: Any, sink: list[str]) -> None:
    try:
        data = stream.read() if stream is not None else ""
    except Exception:
        log.debug("STT pipe read failed", exc_info=True)
        data = ""
    sink.append(data if isinstance(data, str) else "")


def _emit(on_status: Callable[[str], None] | None, message: str) -> None:
    log.info("%s", message)
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception:
        log.exception("STT status callback failed")


def _run_cmd(
    cmd: list[str],
    timeout: float,
    *,
    stop_checker: Callable[[], bool] | None = None,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run *cmd* and return when it exits or *timeout* elapses."""
    if stop_checker is not None and stop_checker():
        raise SttStopped()
    run_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "stdin": subprocess.DEVNULL,
        "env": scrub_subprocess_env(dict(os.environ)),
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(wrap_command_for_sandbox(cmd), **run_kwargs)
    except OSError as exc:
        log.warning("STT command failed (%s): %s", cmd[0], exc)
        return None

    from plugin.framework.worker_pool import run_in_background

    # Drain both pipes. A full stdout pipe deadlocks the child while we wait.
    out_parts: list[str] = []
    err_parts: list[str] = []
    handles: list[Any] = []
    stdout_pipe = proc.stdout
    stderr_pipe = proc.stderr
    if stdout_pipe is not None:
        handles.append(
            run_in_background(lambda stream=stdout_pipe, sink=out_parts: _read_stt_pipe(stream, sink), name="stt-stdout", dedicated=True)
        )
    if stderr_pipe is not None:
        handles.append(
            run_in_background(lambda stream=stderr_pipe, sink=err_parts: _read_stt_pipe(stream, sink), name="stt-stderr", dedicated=True)
        )

    def _join_drains() -> None:
        for handle in handles:
            try:
                handle.join(timeout=1.0)
            except Exception:
                log.debug("STT pipe drain join failed", exc_info=True)

    try:
        _arm_stt_cancel(proc, on_spawn, stop_checker)
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("STT command timed out (%s)", cmd[0])
                terminate_stt_process(proc)
                return None
            try:
                proc.wait(timeout=min(_STT_POLL_SEC, remaining))
            except subprocess.TimeoutExpired:
                continue
        # Join before reading the lists. ``return`` evaluates its expression
        # before ``finally``, so a join only in ``finally`` would drop stdout.
        _reap_stt_process(proc)
        _join_drains()
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode if proc.returncode is not None else 1,
            "".join(out_parts),
            "".join(err_parts),
        )
    finally:
        if proc.poll() is None:
            terminate_stt_process(proc)
            _reap_stt_process(proc)
        _join_drains()


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
    *,
    stop_checker: Callable[[], bool] | None = None,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> str:
    if stop_checker is not None and stop_checker():
        raise SttStopped()
    py_exe = resolve_stt_python()
    if not py_exe:
        raise ConfigError(_missing_venv_message())
    if not os.path.isfile(_WHISPER_SCRIPT):
        raise ConfigError(_("Local Whisper script is missing from the extension."))
    if not ensure_faster_whisper(py_exe):
        raise ConfigError(_missing_package_message())

    # The child downloads inside WhisperModel. Mention it only when model.bin
    # is not already in the hub cache — a later Record of the same size must
    # not claim another download. The download does not stream progress back.
    _emit(on_status, _local_whisper_status(model_name))
    cmd_extra: dict[str, Any] = {}
    if on_spawn is not None:
        cmd_extra["on_spawn"] = on_spawn
    if stop_checker is not None:
        cmd_extra["stop_checker"] = stop_checker
    completed = _run_cmd(
        [py_exe, _WHISPER_SCRIPT, "--wav", wav_path, "--model", model_name],
        _TRANSCRIBE_TIMEOUT_SEC,
        **cmd_extra,
    )
    if completed is None:
        if _local_whisper_weights_cached(model_name):
            raise ConfigError(
                _(
                    "Local Whisper timed out or could not start. "
                    "Try again, or pick a smaller Local Model in Settings → Speech."
                )
            )
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
    stop_checker: Callable[[], bool] | None = None,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> str:
    """Transcribe ``wav_path`` with the configured STT provider.

    ``model`` is the endpoint STT id (``audio.stt_model``). Local Whisper
    ignores it and uses ``audio.stt_local_model``. Endpoint calls
    ``client.transcribe_audio`` and does not spawn the venv.
    """
    if stop_checker is not None and stop_checker():
        raise SttStopped()
    if uses_local_stt():
        local_extra: dict[str, Any] = {}
        if on_spawn is not None:
            local_extra["on_spawn"] = on_spawn
        if stop_checker is not None:
            local_extra["stop_checker"] = stop_checker
        return _transcribe_local(
            wav_path,
            get_stt_local_model(),
            on_status,
            **local_extra,
        )
    if client is None or not hasattr(client, "transcribe_audio"):
        raise ConfigError(_("No language-model client is available for endpoint speech-to-text."))
    return str(client.transcribe_audio(wav_path, model=model) or "")
