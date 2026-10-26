# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""On-demand download and path resolution helpers for TTS models."""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from plugin.framework.errors import best_effort

__all__ = [
    "DownloadOutcome",
    "_download_to",
    "_failed_model_downloads",
    "_resolve_kokoro_model_files",
    "_resolve_piper_model_file",
    "is_download_failed",
    "remember_download_failed",
]

from plugin.audio.model_cache_paths import (
    KOKORO_MODEL_FILENAME,
    KOKORO_VOICES_FILENAME,
    kokoro_should_download,
    resolve_kokoro_model_paths,
    resolve_piper_voice_paths,
)
from plugin.audio.voice_catalog import (
    PIPER_FALLBACK_VOICE,
    PIPER_VOICE_MODELS,
    voice_short_name,
)
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

_KOKORO_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
)
_PIPER_RELEASE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

KOKORO_ONNX_MIN_BYTES = 200_000_000
KOKORO_ONNX_MAX_BYTES = 500 * 1024 * 1024
KOKORO_VOICES_MIN_BYTES = 10_000_000
KOKORO_VOICES_MAX_BYTES = 80 * 1024 * 1024
PIPER_ONNX_MIN_BYTES = 100_000
PIPER_ONNX_MAX_BYTES = 200 * 1024 * 1024
PIPER_JSON_MIN_BYTES = 32
PIPER_JSON_MAX_BYTES = 2 * 1024 * 1024

KOKORO_ONNX_DEADLINE = 20 * 60.0
KOKORO_VOICES_DEADLINE = 10 * 60.0
PIPER_ONNX_DEADLINE = 10 * 60.0
PIPER_JSON_DEADLINE = 2 * 60.0


class DownloadOutcome(enum.Enum):
    OK = "OK"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class _DownloadSlot:
    def __init__(self) -> None:
        self.done_event: threading.Event = threading.Event()
        self.outcome: DownloadOutcome | None = None


_download_slots: dict[str, _DownloadSlot] = {}
_download_slots_lock = threading.Lock()

# generation -> set of failed model download keys.
# Remembered per utterance generation so offline/failed downloads do not retry per sentence.
_failed_model_downloads: dict[int, set[str]] = {}


def remember_download_failed(generation: int | None, key: str) -> None:
    """Record a failed download key for the current speech generation."""
    if generation is None:
        return
    from plugin.audio.tts_service import _speech_generation, _speech_lock

    with _speech_lock:
        if generation != _speech_generation:
            return
        _failed_model_downloads.setdefault(generation, set()).add(key)


def is_download_failed(generation: int | None, key: str) -> bool:
    """True if key already failed to download during this speech generation."""
    if generation is None:
        return False
    from plugin.audio.tts_service import _speech_lock

    with _speech_lock:
        return key in _failed_model_downloads.get(generation, set())


def clear_generation_downloads(generation: int) -> None:
    """Drop remembered download failures for one utterance; the caller must hold
    tts_service._speech_lock; stop_speech and _end_utterance do; the lock is not
    taken here because it is not reentrant."""
    _failed_model_downloads.pop(generation, None)


def _notify_tts_status(
    text: str,
    on_status: Callable[[str], None] | None,
    *,
    progress: bool = False,
) -> None:
    """Notify TTS status."""
    if not text:
        return
    log.info("%s", text)
    if on_status is None:
        return
    try:
        if progress:
            prog = getattr(on_status, "progress", None)
            if callable(prog):
                prog(text)
                return
        on_status(text)
    except Exception:
        log.debug("TTS status callback failed", exc_info=True)


def _clear_tts_status(on_status: Callable[[str], None] | None) -> None:
    """Dismiss a transient download/progress status when the callback supports it."""
    if on_status is None:
        return
    clear = getattr(on_status, "clear", None)
    if callable(clear):
        best_effort(clear, "TTS status clear failed")


def _unlink_temp(path: str | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            os.remove(path)


def _sleep_cancellable(duration: float, cancelled: Callable[[], bool] | None) -> bool:
    end = time.monotonic() + duration
    while time.monotonic() < end:
        if cancelled and cancelled():
            return False
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    return not (cancelled and cancelled())


def _check_disk_space(dest: str, min_bytes: int) -> bool:
    if min_bytes <= 1:
        return True
    try:
        import shutil

        check_dir = os.path.dirname(dest)
        while check_dir and not os.path.exists(check_dir):
            parent = os.path.dirname(check_dir)
            if parent == check_dir:
                break
            check_dir = parent
        if not check_dir or not os.path.exists(check_dir):
            check_dir = "."
        usage = shutil.disk_usage(check_dir)
        return usage.free >= min_bytes
    except OSError:
        return True


def _download_to(
    url: str,
    dest: str,
    timeout: float = 30.0,
    cancelled: Callable[[], bool] | None = None,
    min_bytes: int = 1,
    max_bytes: int | None = None,
    deadline_seconds: float = 600.0,
    on_status: Callable[[str], None] | None = None,
) -> DownloadOutcome:
    """Download a file from url to dest using chunked reads with cancellation and single-flight lock."""
    if cancelled and cancelled():
        return DownloadOutcome.CANCELLED

    deadline = time.monotonic() + deadline_seconds
    canon_dest = os.path.abspath(dest)

    while True:
        if cancelled and cancelled():
            return DownloadOutcome.CANCELLED
        if time.monotonic() > deadline:
            return DownloadOutcome.FAILED

        with _download_slots_lock:
            slot = _download_slots.get(canon_dest)
            if slot is None:
                slot = _DownloadSlot()
                _download_slots[canon_dest] = slot
                is_owner = True
            else:
                is_owner = False

        if is_owner:
            break

        # Waiter loop: check waiter's own cancelled() every 0.2s
        while True:
            if cancelled and cancelled():
                return DownloadOutcome.CANCELLED
            if time.monotonic() > deadline:
                return DownloadOutcome.FAILED
            if slot.done_event.wait(0.2):
                if slot.outcome is not None:
                    return slot.outcome
                # Owner cancelled without outcome: retry outer loop to become owner or wait on new owner
                break

    # Current thread owns the download slot
    outcome = DownloadOutcome.FAILED
    try:
        outcome = _download_to_inner(
            url,
            dest,
            timeout=timeout,
            cancelled=cancelled,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            deadline=deadline,
            on_status=on_status,
        )
        return outcome
    finally:
        with _download_slots_lock:
            _download_slots.pop(canon_dest, None)
            if outcome is not DownloadOutcome.CANCELLED:
                slot.outcome = outcome
            slot.done_event.set()


def _download_to_inner(
    url: str,
    dest: str,
    timeout: float,
    cancelled: Callable[[], bool] | None,
    min_bytes: int,
    max_bytes: int | None,
    deadline: float,
    on_status: Callable[[str], None] | None,
) -> DownloadOutcome:
    if min_bytes > 1 and not _check_disk_space(dest, min_bytes):
        log.warning("Not enough disk space for %s (need %d bytes)", dest, min_bytes)
        return DownloadOutcome.FAILED

    dest_dir = os.path.dirname(dest)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    for attempt in range(3):
        if cancelled and cancelled():
            return DownloadOutcome.CANCELLED
        if time.monotonic() > deadline:
            return DownloadOutcome.FAILED

        if attempt > 0:
            delay = 0.5 if attempt == 1 else 1.5
            if not _sleep_cancellable(delay, cancelled):
                return DownloadOutcome.CANCELLED

        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix="writeragent_tts_",
                suffix=".tmp",
                dir=dest_dir or None,
            )
            os.close(fd)

            req = urllib.request.Request(url, headers={"User-Agent": "WriterAgent/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = getattr(resp, "headers", None)
                ct: str | None = None
                cl: str | None = None
                if headers is not None:
                    ct = headers.get("Content-Type")
                    cl = headers.get("Content-Length")
                elif hasattr(resp, "getheader"):
                    ct = resp.getheader("Content-Type")
                    cl = resp.getheader("Content-Length")

                if ct:
                    ct_base = ct.split(";", 1)[0].strip().lower()
                    is_json_dest = dest.endswith(".json")
                    if (
                        ct_base.startswith("text/")
                        or "html" in ct_base
                        or ct_base == "application/xhtml+xml"
                        or (ct_base == "application/json" and not is_json_dest)
                    ):
                        log.warning("Rejected unexpected content-type %s for %s", ct, url)
                        _unlink_temp(tmp_path)
                        return DownloadOutcome.FAILED

                expected_len: int | None = None
                if cl:
                    try:
                        expected_len = int(cl.strip())
                    except (ValueError, TypeError):
                        expected_len = None

                # Set 1.0s socket timeout so cancellation can be polled frequently
                sock = getattr(resp, "sock", None)
                if sock is None:
                    fp = getattr(resp, "fp", None)
                    if fp is not None:
                        raw_io = getattr(fp, "raw", None)
                        sock = getattr(raw_io, "_sock", None) if raw_io is not None else getattr(fp, "_sock", None)
                if sock is not None and hasattr(sock, "settimeout"):
                    with contextlib.suppress(Exception):
                        sock.settimeout(1.0)

                downloaded = 0
                last_progress = 0.0
                chunk_size = 256 * 1024

                with open(tmp_path, "wb") as f_out:
                    while True:
                        if cancelled and cancelled():
                            _unlink_temp(tmp_path)
                            return DownloadOutcome.CANCELLED
                        if time.monotonic() > deadline:
                            _unlink_temp(tmp_path)
                            return DownloadOutcome.FAILED

                        try:
                            chunk = resp.read(chunk_size)
                        except (TimeoutError, socket.timeout):
                            if cancelled and cancelled():
                                _unlink_temp(tmp_path)
                                return DownloadOutcome.CANCELLED
                            if time.monotonic() > deadline:
                                _unlink_temp(tmp_path)
                                return DownloadOutcome.FAILED
                            continue

                        if not chunk:
                            break
                        f_out.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()
                        if expected_len and expected_len > 0 and (now - last_progress >= 0.25):
                            percent = int(downloaded * 100 / expected_len)
                            if not (cancelled and cancelled()):
                                _notify_tts_status(
                                    _("Downloading… {0}%").format(percent),
                                    on_status,
                                    progress=True,
                                )
                            last_progress = now

                    f_out.flush()
                    os.fsync(f_out.fileno())

                if expected_len is not None and downloaded != expected_len:
                    log.warning("Download size mismatch for %s: got %d, expected %d", url, downloaded, expected_len)
                    _unlink_temp(tmp_path)
                    return DownloadOutcome.FAILED

                if downloaded < min_bytes or (max_bytes is not None and downloaded > max_bytes):
                    log.warning("Download size out of bounds for %s: got %d (min=%d, max=%s)", url, downloaded, min_bytes, max_bytes)
                    _unlink_temp(tmp_path)
                    return DownloadOutcome.FAILED

                if dest_dir:
                    try:
                        dir_fd = os.open(dest_dir, os.O_RDONLY)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except OSError:
                        pass

                try:
                    os.replace(tmp_path, dest)
                except PermissionError:
                    if os.path.exists(dest) and os.path.getsize(dest) >= min_bytes:
                        _unlink_temp(tmp_path)
                        return DownloadOutcome.OK
                    _unlink_temp(tmp_path)
                    raise

                return DownloadOutcome.OK

        except urllib.error.HTTPError as exc:
            _unlink_temp(tmp_path)
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                log.info("HTTP %s downloading %s; retrying", exc.code, url)
                continue
            log.warning("Download failed for %s -> %s: %s", url, dest, exc)
            return DownloadOutcome.FAILED
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            _unlink_temp(tmp_path)
            if attempt < 2:
                log.info("Network error downloading %s (%s); retrying", url, exc)
                continue
            log.warning("Download failed for %s -> %s: %s", url, dest, exc)
            return DownloadOutcome.FAILED
        except InterruptedError:
            _unlink_temp(tmp_path)
            return DownloadOutcome.CANCELLED
        except Exception as exc:
            _unlink_temp(tmp_path)
            log.warning("Download failed for %s -> %s: %s", url, dest, exc)
            return DownloadOutcome.FAILED

    return DownloadOutcome.FAILED


def _resolve_kokoro_model_files(
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Resolve paths to Kokoro ONNX model and voices file, downloading if missing."""
    model_path, voices_path = resolve_kokoro_model_paths()
    model_s = os.fspath(model_path)
    voices_s = os.fspath(voices_path)

    env_m = os.environ.get("KOKORO_MODEL_PATH")
    env_v = os.environ.get("KOKORO_VOICES_PATH")
    is_env_m = bool(env_m and os.fspath(env_m) == model_s and os.path.exists(model_s))
    is_env_v = bool(env_v and os.fspath(env_v) == voices_s and os.path.exists(voices_s))

    if is_env_m and os.path.getsize(model_s) < KOKORO_ONNX_MIN_BYTES:
        log.warning("KOKORO_MODEL_PATH %s is smaller than minimum size", model_s)
    if is_env_v and os.path.getsize(voices_s) < KOKORO_VOICES_MIN_BYTES:
        log.warning("KOKORO_VOICES_PATH %s is smaller than minimum size", voices_s)

    model_ok = is_env_m or (os.path.exists(model_s) and os.path.getsize(model_s) >= KOKORO_ONNX_MIN_BYTES)
    voices_ok = is_env_v or (os.path.exists(voices_s) and os.path.getsize(voices_s) >= KOKORO_VOICES_MIN_BYTES)

    if model_ok and voices_ok:
        return model_s, voices_s

    if is_download_failed(generation, "kokoro") or (cancelled and cancelled()):
        return model_s, voices_s

    needs_voices = (not voices_ok and not is_env_v) or kokoro_should_download(voices_path, "KOKORO_VOICES_PATH")
    needs_model = (not model_ok and not is_env_m) or kokoro_should_download(model_path, "KOKORO_MODEL_PATH")

    if not needs_voices and not needs_model:
        return model_s, voices_s

    needed_disk = (KOKORO_VOICES_MIN_BYTES if needs_voices else 0) + (KOKORO_ONNX_MIN_BYTES if needs_model else 0)
    target_dest = voices_s if needs_voices else model_s
    if needed_disk > 1 and not _check_disk_space(target_dest, needed_disk):
        remember_download_failed(generation, "kokoro")
        _notify_tts_status(_("Not enough disk space to download the voice model."), on_status, progress=True)
        _clear_tts_status(on_status)
        return model_s, voices_s

    _notify_tts_status(_("Downloading Kokoro voice model…"), on_status, progress=True)
    failed = False

    if needs_voices:
        log.info("Downloading Kokoro voices to %s...", voices_s)
        outcome = _download_to(
            f"{_KOKORO_RELEASE_BASE}/{KOKORO_VOICES_FILENAME}",
            voices_s,
            timeout=30.0,
            cancelled=cancelled,
            min_bytes=KOKORO_VOICES_MIN_BYTES,
            max_bytes=KOKORO_VOICES_MAX_BYTES,
            deadline_seconds=KOKORO_VOICES_DEADLINE,
            on_status=on_status,
        )
        if outcome is DownloadOutcome.CANCELLED:
            _clear_tts_status(on_status)
            return model_s, voices_s
        elif outcome is not DownloadOutcome.OK:
            failed = True

    if needs_model and not failed:
        log.info("Downloading Kokoro ONNX model to %s...", model_s)
        outcome = _download_to(
            f"{_KOKORO_RELEASE_BASE}/{KOKORO_MODEL_FILENAME}",
            model_s,
            timeout=30.0,
            cancelled=cancelled,
            min_bytes=KOKORO_ONNX_MIN_BYTES,
            max_bytes=KOKORO_ONNX_MAX_BYTES,
            deadline_seconds=KOKORO_ONNX_DEADLINE,
            on_status=on_status,
        )
        if outcome is DownloadOutcome.CANCELLED:
            _clear_tts_status(on_status)
            return model_s, voices_s
        elif outcome is not DownloadOutcome.OK:
            failed = True

    if failed:
        remember_download_failed(generation, "kokoro")
        _notify_tts_status(_("Couldn't download Kokoro; using OS speech"), on_status, progress=True)
        _clear_tts_status(on_status)
    else:
        _clear_tts_status(on_status)

    return model_s, voices_s


def _download_piper_pair(
    voice_id: str,
    onnx: str,
    json_path: str,
    cancelled: Callable[[], bool] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> DownloadOutcome:
    model_info = PIPER_VOICE_MODELS.get(voice_id)
    if not model_info:
        return DownloadOutcome.FAILED
    rel_onnx, rel_json = model_info[0], model_info[1]

    dest_dir = os.path.dirname(onnx)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    fd1, onnx_tmp = tempfile.mkstemp(prefix="writeragent_piper_", suffix=".tmp", dir=dest_dir or None)
    os.close(fd1)
    fd2, json_tmp = tempfile.mkstemp(prefix="writeragent_piper_", suffix=".tmp", dir=dest_dir or None)
    os.close(fd2)

    outcome1 = _download_to(
        f"{_PIPER_RELEASE_BASE}/{rel_onnx}",
        onnx_tmp,
        timeout=30.0,
        cancelled=cancelled,
        min_bytes=PIPER_ONNX_MIN_BYTES,
        max_bytes=PIPER_ONNX_MAX_BYTES,
        deadline_seconds=PIPER_ONNX_DEADLINE,
        on_status=on_status,
    )
    if outcome1 is not DownloadOutcome.OK:
        _unlink_temp(onnx_tmp)
        _unlink_temp(json_tmp)
        return outcome1

    outcome2 = _download_to(
        f"{_PIPER_RELEASE_BASE}/{rel_json}",
        json_tmp,
        timeout=30.0,
        cancelled=cancelled,
        min_bytes=PIPER_JSON_MIN_BYTES,
        max_bytes=PIPER_JSON_MAX_BYTES,
        deadline_seconds=PIPER_JSON_DEADLINE,
        on_status=on_status,
    )
    if outcome2 is not DownloadOutcome.OK:
        _unlink_temp(onnx_tmp)
        _unlink_temp(json_tmp)
        return outcome2

    if cancelled and cancelled():
        _unlink_temp(onnx_tmp)
        _unlink_temp(json_tmp)
        return DownloadOutcome.CANCELLED

    try:
        os.replace(onnx_tmp, onnx)
    except OSError:
        _unlink_temp(onnx_tmp)
        _unlink_temp(json_tmp)
        return DownloadOutcome.FAILED

    try:
        os.replace(json_tmp, json_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(onnx)
        _unlink_temp(json_tmp)
        return DownloadOutcome.FAILED

    return DownloadOutcome.OK


def _resolve_piper_model_file(
    voice: str,
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str | None:
    """Resolve path to Piper ONNX model file, downloading on demand if missing."""
    from plugin.audio.tts_voices import clean_voice_name

    clean_v = clean_voice_name(voice)
    if not clean_v:
        clean_v = PIPER_FALLBACK_VOICE

    if os.path.isabs(clean_v) and os.path.exists(clean_v):
        return clean_v

    voice_file, json_file = resolve_piper_voice_paths(clean_v)
    voice_s = os.fspath(voice_file)
    json_s = os.fspath(json_file)

    if (
        os.path.exists(voice_s)
        and os.path.getsize(voice_s) >= PIPER_ONNX_MIN_BYTES
        and os.path.exists(json_s)
        and os.path.getsize(json_s) >= PIPER_JSON_MIN_BYTES
    ):
        return voice_s

    if cancelled and cancelled():
        return None

    download_failed = False
    short = voice_short_name(clean_v)

    if clean_v in PIPER_VOICE_MODELS and not is_download_failed(generation, clean_v):
        if not _check_disk_space(voice_s, PIPER_ONNX_MIN_BYTES + PIPER_JSON_MIN_BYTES):
            remember_download_failed(generation, clean_v)
            _notify_tts_status(_("Not enough disk space to download the voice model."), on_status, progress=True)
            _clear_tts_status(on_status)
            return None

        _notify_tts_status(_("Downloading Piper voice {0}…").format(short), on_status, progress=True)
        log.info("Downloading Piper voice model '%s' to %s...", clean_v, voice_s)
        outcome = _download_piper_pair(clean_v, voice_s, json_s, cancelled=cancelled, on_status=on_status)
        if outcome is DownloadOutcome.OK:
            _clear_tts_status(on_status)
            return voice_s
        elif outcome is DownloadOutcome.CANCELLED:
            _clear_tts_status(on_status)
            return None

        download_failed = True
        remember_download_failed(generation, clean_v)
        log.warning("Could not auto-download Piper voice '%s'", clean_v)

    default_voice_file, default_json = resolve_piper_voice_paths(PIPER_FALLBACK_VOICE)
    default_voice_s = os.fspath(default_voice_file)
    default_json_s = os.fspath(default_json)
    lessac_ready = (
        os.path.exists(default_voice_s)
        and os.path.getsize(default_voice_s) >= PIPER_ONNX_MIN_BYTES
        and os.path.exists(default_json_s)
        and os.path.getsize(default_json_s) >= PIPER_JSON_MIN_BYTES
    )
    other_voice = clean_v != PIPER_FALLBACK_VOICE

    if lessac_ready:
        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status, progress=True)
            _clear_tts_status(on_status)
        return default_voice_s

    if not is_download_failed(generation, PIPER_FALLBACK_VOICE):
        if cancelled and cancelled():
            return None
        if not _check_disk_space(default_voice_s, PIPER_ONNX_MIN_BYTES + PIPER_JSON_MIN_BYTES):
            remember_download_failed(generation, PIPER_FALLBACK_VOICE)
            _notify_tts_status(_("Not enough disk space to download the voice model."), on_status, progress=True)
            _clear_tts_status(on_status)
            return None

        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status, progress=True)
            _clear_tts_status(on_status)
        else:
            fallback_short = voice_short_name(PIPER_FALLBACK_VOICE)
            _notify_tts_status(_("Downloading Piper voice {0}…").format(fallback_short), on_status, progress=True)
        log.info("Downloading Piper default voice model to %s...", default_voice_s)
        outcome_fallback = _download_piper_pair(PIPER_FALLBACK_VOICE, default_voice_s, default_json_s, cancelled=cancelled, on_status=on_status)
        if outcome_fallback is DownloadOutcome.OK:
            _clear_tts_status(on_status)
            return default_voice_s
        elif outcome_fallback is DownloadOutcome.CANCELLED:
            _clear_tts_status(on_status)
            return None

        remember_download_failed(generation, PIPER_FALLBACK_VOICE)
        log.warning("Could not auto-download Piper default voice model")

    failed_name = short if download_failed else voice_short_name(PIPER_FALLBACK_VOICE)
    _notify_tts_status(_("Couldn't download {0}; using OS speech").format(failed_name), on_status, progress=True)
    _clear_tts_status(on_status)
    return None
