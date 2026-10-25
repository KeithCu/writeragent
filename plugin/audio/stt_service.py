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

from dataclasses import dataclass
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable

from plugin.framework.config import get_config
from plugin.framework.errors import ConfigError, best_effort
from plugin.framework.i18n import _
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

# Shown when the venv cannot ``import faster_whisper``. Record does not run it.
_FASTER_WHISPER_PIP_INSTALL = 'uv pip install faster-whisper "av<14"'

STT_PROVIDER_ENDPOINT = "endpoint"
STT_PROVIDER_LOCAL = "local"

# faster-whisper size aliases. Each downloads Systran/faster-whisper-<size>
# into the Hugging Face cache the first time that size is used.
LOCAL_STT_MODELS = ("tiny", "base", "small", "medium")
DEFAULT_STT_LOCAL_MODEL = "base"

# First run may download weights (base is ~150 MB; medium is ~1.5 GB) and then
# transcribe. The 900 s bound is a hung download or decode, not Stop. Stop during
# transcription still does not kill the child.
_TRANSCRIBE_TIMEOUT_SEC = 900.0
# How often the host polls while the child is alive so the timeout bound can fire.
_STT_POLL_SEC = 0.2

_WHISPER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper_transcribe.py")


@dataclass(frozen=True)
class SttCommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    spawn_failed: bool = False


def normalize_stt_provider(raw: object) -> str:
    """Map settings text and legacy aliases to ``endpoint`` or ``local``.

    Missing and unknown values stay ``endpoint`` so an old config (no
    ``audio.stt_provider``) keeps POST ``/v1/audio/transcriptions``.
    """
    text = str(raw or "").strip().lower()
    if text == "local" or "whisper" in text:
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
    if " " not in text and len(text) <= 200 and not text.startswith("{") and not text.startswith("-"):
        return text
    return DEFAULT_STT_LOCAL_MODEL


def stt_controls_enabled(provider: str) -> tuple[bool, bool]:
    """Return ``(endpoint Audio Model enabled, local model enabled)``.

    Speech settings shows one of the two, matching TTS Model (only for the
    endpoint provider).
    """
    local = normalize_stt_provider(provider) == STT_PROVIDER_LOCAL
    return (not local, local)


def _config_str_or_empty(key: str) -> str:
    """Read a string config value, returning empty string on missing key."""
    try:
        val = get_config(key)
        return str(val) if val is not None else ""
    except ConfigError:
        return ""


def get_stt_provider() -> str:
    """``audio.stt_provider``, or ``endpoint`` when the key is absent."""
    return normalize_stt_provider(_config_str_or_empty("audio.stt_provider"))


def get_stt_local_model() -> str:
    """``audio.stt_local_model``, default ``base``."""
    return normalize_stt_local_model(_config_str_or_empty("audio.stt_local_model"))


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


def resolve_stt_python() -> str | None:
    """Venv interpreter from Settings → Python, or None when it is not set."""
    venv_dir = _config_str_or_empty("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def terminate_stt_process(proc: subprocess.Popen[str] | None) -> None:
    """Kill a Whisper or probe child. A second call is a no-op.

    ``SIGKILL`` (``kill``) so Stop does not wait on a 900s ``subprocess.run``.
    The caller reaps with ``wait``. Do not ``wait`` here: this runs on the UI
    thread from ``SendCancellation.cancel``.
    """
    if proc is None or proc.poll() is not None:
        return
    best_effort(proc.kill, "STT kill failed")


def _reap_stt_process(proc: subprocess.Popen[str]) -> None:
    best_effort(lambda: proc.wait(timeout=1.0), "STT reap failed")


def _close_stt_pipe(stream: Any) -> None:
    if stream is not None:
        best_effort(stream.close, "STT pipe close failed")


def _read_stt_pipe(stream: Any, sink: list[str]) -> None:
    data = best_effort(stream.read, "STT pipe read failed", default="") if stream is not None else ""
    sink.append(data if isinstance(data, str) else "")


def _run_cmd(
    cmd: list[str],
    timeout: float,
    *,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> SttCommandResult:
    """Run *cmd* and return when it exits or *timeout* elapses."""
    scrubbed_env = scrub_subprocess_env(dict(os.environ))
    run_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "stdin": subprocess.DEVNULL,
        "env": scrubbed_env,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(wrap_command_for_sandbox(cmd, env=scrubbed_env, watch_bus=True), **run_kwargs)
    except OSError as exc:
        log.warning("STT command failed (%s): %s", cmd[0], exc)
        return SttCommandResult(None, "", str(exc), spawn_failed=True)

    if on_spawn is not None:
        try:
            on_spawn(proc)
        except Exception:
            log.exception("STT on_spawn failed")

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
            best_effort(lambda: handle.join(timeout=1.0), "STT pipe drain join failed")

    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("STT command timed out (%s)", cmd[0])
                timed_out = True
                terminate_stt_process(proc)
                break
            try:
                proc.wait(timeout=min(_STT_POLL_SEC, remaining))
            except subprocess.TimeoutExpired:
                continue
        # Join before reading the lists. ``return`` evaluates its expression
        # before ``finally``, so a join only in ``finally`` would drop stdout.
        _reap_stt_process(proc)
        _join_drains()
        out = "".join(out_parts)
        err = "".join(err_parts)
        return SttCommandResult(
            proc.returncode,
            out,
            err,
            timed_out=timed_out,
            spawn_failed=False,
        )
    finally:
        if proc.poll() is None:
            terminate_stt_process(proc)
            _reap_stt_process(proc)
        _join_drains()
        # A grandchild that inherited the write end keeps the dedicated drain
        # thread blocked in read() after the 1 s join, and that thread holds the
        # pipe so GC never closes it. Closing the read end unblocks the thread.
        _close_stt_pipe(proc.stdout)
        _close_stt_pipe(proc.stderr)


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


def _local_timeout_message(cached: bool) -> str:
    if cached:
        return _(
            "Local Whisper timed out. "
            "Try again, or pick a smaller Local Model in Settings → Speech."
        )
    return _(
        "Local Whisper timed out. The first run downloads model weights; "
        "try again, or pick a smaller Local Model in Settings → Speech."
    )


def _emit(on_status: Callable[[str], None] | None, message: str) -> None:
    log.info("%s", message)
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception as e:
        log.debug("on_status callback failed: %s", e)


def _transcribe_local(
    wav_path: str,
    model_name: str,
    on_status: Callable[[str], None] | None = None,
    *,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> str:
    py_exe = resolve_stt_python()
    if not py_exe:
        raise ConfigError(
            _(
                "Local Whisper runs in the Settings → Python venv, not LibreOffice's Python. "
                "Set the venv, then install with: {0}"
            ).format(_FASTER_WHISPER_PIP_INSTALL)
        )
    if not os.path.isfile(_WHISPER_SCRIPT):
        raise ConfigError(_("Local Whisper script is missing from the extension."))

    # What was wrong: local Whisper status updates were dropped, so weight download
    # was not surfaced to the sidebar while waiting for transcription.
    # How it happened: on_status callback and _emit helper were removed during cleanup.
    # Why this change: emit _local_whisper_status so the user knows weights are downloading.
    _emit(on_status, _local_whisper_status(model_name))
    cmd = [py_exe, _WHISPER_SCRIPT, "--wav", wav_path, f"--model={model_name}"]
    completed = _run_cmd(cmd, _TRANSCRIBE_TIMEOUT_SEC, on_spawn=on_spawn)

    returncode = getattr(completed, "returncode", None)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    timed_out = bool(getattr(completed, "timed_out", False))
    spawn_failed = bool(getattr(completed, "spawn_failed", False))

    tail = stderr.strip()[-500:]

    if spawn_failed:
        raise ConfigError(_("Local Whisper could not start ({0}).").format(tail))
    if timed_out:
        raise ConfigError(_local_timeout_message(_local_whisper_weights_cached(model_name)))

    try:
        return parse_whisper_stdout(stdout)
    except ConfigError as exc:
        if returncode not in (0, None) or tail:
            log.warning("Local Whisper exited %s: %s", returncode, tail)
        msg = str(exc)
        if "faster-whisper is not installed" in msg:
            raise ConfigError(
                _(
                    "faster-whisper is not installed in the Settings → Python venv. "
                    "Install with: {0}. Settings → Python Test lists it under Audio optional packages."
                ).format(_FASTER_WHISPER_PIP_INSTALL)
            ) from exc
        if "did not return a transcript" in msg:
            detail = tail or "no error output"
            err_msg = f"Local Whisper exited {returncode}: {detail}."
            if returncode is not None and returncode < 0:
                err_msg += " The process was killed; try a smaller Local Model in Settings → Speech."
            raise ConfigError(err_msg) from exc
        raise


def transcribe(
    wav_path: str,
    *,
    client: Any = None,
    model: str | None = None,
    on_status: Callable[[str], None] | None = None,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
) -> str:
    """Transcribe ``wav_path`` with the configured STT provider.

    ``model`` is the endpoint STT id (``audio.stt_model``). Local Whisper
    ignores it and uses ``audio.stt_local_model``. Endpoint calls
    ``client.transcribe_audio`` and does not spawn the venv.
    """
    if uses_local_stt():
        return _transcribe_local(
            wav_path,
            get_stt_local_model(),
            on_status,
            on_spawn=on_spawn,
        )
    if client is None or not hasattr(client, "transcribe_audio"):
        raise ConfigError(_("No language-model client is available for endpoint speech-to-text."))
    return str(client.transcribe_audio(wav_path, model=model, status_callback=on_status) or "")
