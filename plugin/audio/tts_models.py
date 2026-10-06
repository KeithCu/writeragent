# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""On-demand download and path resolution helpers for TTS models."""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Callable

__all__ = [
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

# generation -> set of failed model download keys.
# Remembered per utterance generation so offline/failed downloads do not retry per sentence.
_failed_model_downloads: dict[int, set[str]] = {}


def remember_download_failed(generation: int | None, key: str) -> None:
    """Record a failed download key for the current speech generation."""
    if generation is None:
        return
    _failed_model_downloads.setdefault(generation, set()).add(key)


def is_download_failed(generation: int | None, key: str) -> bool:
    """True if key already failed to download during this speech generation."""
    if generation is None:
        return False
    return key in _failed_model_downloads.get(generation, set())


def clear_generation_downloads(generation: int) -> None:
    """Clean up remembered download failures when an utterance finishes."""
    _failed_model_downloads.pop(generation, None)


def _notify_tts_status(
    text: str,
    on_status: Callable[[str], None] | None,
    *,
    progress: bool = False,
) -> None:
    if on_status is None or not text:
        return
    on_status(text)


def _clear_tts_status(on_status: Callable[[str], None] | None) -> None:
    """Dismiss a transient download/progress status when the callback supports it."""
    if on_status is None:
        return
    clear = getattr(on_status, "clear", None)
    if not callable(clear):
        return
    try:
        clear()
    except Exception:
        pass


def _download_to(
    url: str,
    dest: str,
    timeout: float = 30.0,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Download a file from url to dest using chunked reads with cancellation support.

    Writes to dest + '.tmp' and renames to dest on success.
    Checks cancelled() periodically and removes .tmp on failure or cancel.
    """
    if cancelled and cancelled():
        return False
    tmp_path = dest + ".tmp"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "WriterAgent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp_path, "wb") as f_out:
                chunk_size = 64 * 1024
                while True:
                    if cancelled and cancelled():
                        log.info("Download of %s cancelled", url)
                        raise InterruptedError("Download cancelled")
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f_out.write(chunk)
        os.replace(tmp_path, dest)
        return True
    except Exception as exc:
        if not isinstance(exc, InterruptedError):
            log.warning("Download failed for %s -> %s: %s", url, dest, exc)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False


def _resolve_kokoro_model_files(
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Resolve paths to Kokoro ONNX model and voices file, downloading if missing.

    A failure is remembered for the generation so subsequent sentences in the same
    utterance do not re-attempt the download.
    """
    model_path, voices_path = resolve_kokoro_model_paths(
        KOKORO_MODEL_FILENAME, KOKORO_VOICES_FILENAME,
    )
    model_s = os.fspath(model_path)
    voices_s = os.fspath(voices_path)

    if os.path.exists(model_s) and os.path.exists(voices_s):
        return model_s, voices_s

    if is_download_failed(generation, "kokoro") or (cancelled and cancelled()):
        return model_s, voices_s

    needs_voices = kokoro_should_download(voices_path, "KOKORO_VOICES_PATH")
    needs_model = kokoro_should_download(model_path, "KOKORO_MODEL_PATH")
    failed = False
    if needs_voices or needs_model:
        _notify_tts_status(_("Downloading Kokoro voice model…"), on_status, progress=True)
        if needs_voices:
            log.info("Downloading Kokoro voices to %s...", voices_s)
            ok = _download_to(
                f"{_KOKORO_RELEASE_BASE}/{KOKORO_VOICES_FILENAME}",
                voices_s,
                timeout=30.0,
                cancelled=cancelled,
            )
            if not ok:
                failed = True
        if needs_model and not failed:
            log.info("Downloading Kokoro ONNX model to %s...", model_s)
            ok = _download_to(
                f"{_KOKORO_RELEASE_BASE}/{KOKORO_MODEL_FILENAME}",
                model_s,
                timeout=60.0,
                cancelled=cancelled,
            )
            if not ok:
                failed = True

    if failed:
        remember_download_failed(generation, "kokoro")
        _notify_tts_status(_("Couldn't download Kokoro; using OS speech"), on_status, progress=True)
        _clear_tts_status(on_status)
    elif needs_voices or needs_model:
        _clear_tts_status(on_status)

    return model_s, voices_s


def _resolve_piper_model_file(
    voice: str,
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str | None:
    """Resolve path to Piper ONNX model file, downloading on demand if missing.

    Returns the path to the onnx model file, or None if missing/failed.
    """
    from plugin.audio.tts_voices import clean_voice_name

    clean_v = clean_voice_name(voice)
    if not clean_v:
        clean_v = PIPER_FALLBACK_VOICE

    if os.path.isabs(clean_v) and os.path.exists(clean_v):
        return clean_v

    voice_file, json_file = resolve_piper_voice_paths(clean_v)
    voice_s = os.fspath(voice_file)
    json_s = os.fspath(json_file)

    if os.path.exists(voice_s) and os.path.exists(json_s):
        return voice_s

    download_failed = False
    short = voice_short_name(clean_v)

    if clean_v in PIPER_VOICE_MODELS and not is_download_failed(generation, clean_v):
        rel_onnx, rel_json = PIPER_VOICE_MODELS[clean_v][0], PIPER_VOICE_MODELS[clean_v][1]
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        _notify_tts_status(_("Downloading Piper voice {0}…").format(short), on_status, progress=True)
        log.info("Downloading Piper voice model '%s' to %s...", clean_v, voice_s)
        ok_onnx = _download_to(f"{base_url}/{rel_onnx}", voice_s, timeout=60.0, cancelled=cancelled)
        ok_json = ok_onnx and _download_to(f"{base_url}/{rel_json}", json_s, timeout=30.0, cancelled=cancelled)
        if ok_onnx and ok_json:
            _clear_tts_status(on_status)
            return voice_s

        download_failed = True
        remember_download_failed(generation, clean_v)
        log.warning("Could not auto-download Piper voice '%s'", clean_v)
        for p in (voice_s, json_s):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    default_voice_file, default_json = resolve_piper_voice_paths(PIPER_FALLBACK_VOICE)
    default_voice_s = os.fspath(default_voice_file)
    default_json_s = os.fspath(default_json)
    lessac_ready = os.path.exists(default_voice_s) and os.path.exists(default_json_s)
    other_voice = clean_v != PIPER_FALLBACK_VOICE

    if lessac_ready:
        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status, progress=True)
            _clear_tts_status(on_status)
        return default_voice_s

    if not is_download_failed(generation, PIPER_FALLBACK_VOICE):
        rel_onnx, rel_json = PIPER_VOICE_MODELS[PIPER_FALLBACK_VOICE][0], PIPER_VOICE_MODELS[PIPER_FALLBACK_VOICE][1]
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status, progress=True)
            _clear_tts_status(on_status)
        else:
            fallback_short = voice_short_name(PIPER_FALLBACK_VOICE)
            _notify_tts_status(_("Downloading Piper voice {0}…").format(fallback_short), on_status, progress=True)
        log.info("Downloading Piper default voice model to %s...", default_voice_s)
        ok_onnx = _download_to(f"{base_url}/{rel_onnx}", default_voice_s, timeout=60.0, cancelled=cancelled)
        ok_json = ok_onnx and _download_to(f"{base_url}/{rel_json}", default_json_s, timeout=30.0, cancelled=cancelled)
        if ok_onnx and ok_json:
            _clear_tts_status(on_status)
            return default_voice_s

        remember_download_failed(generation, PIPER_FALLBACK_VOICE)
        log.warning("Could not auto-download Piper default voice model")
        for p in (default_voice_s, default_json_s):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    failed_name = short if download_failed else voice_short_name(PIPER_FALLBACK_VOICE)
    _notify_tts_status(_("Couldn't download {0}; using OS speech").format(failed_name), on_status, progress=True)
    _clear_tts_status(on_status)
    return None
