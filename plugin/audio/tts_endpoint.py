# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Endpoint HTTP text-to-speech synthesis (/v1/audio/speech)."""

from __future__ import annotations

import logging
import re
import struct
import sys
from typing import Any, Callable

from plugin.audio.tts_models import _notify_tts_status
from plugin.audio.tts_voices import _resolve_tts_voice
from plugin.framework.client.http_transport import public_target
from plugin.framework.i18n import _

__all__ = [
    "_alternate_tts_response_format",
    "_content_type_is_mp3",
    "_content_type_is_pcm",
    "_content_type_is_wav",
    "_default_endpoint_format",
    "_download_endpoint_speech",
    "_pcm_rate_from_content_type",
    "_pcm_s16le_to_wav",
    "_post_audio_speech",
    "_sniff_audio_format",
    "_speak_endpoint",
    "_speech_failure_message",
    "_speech_read_timeout",
]

log = logging.getLogger(__name__)

_PCM_RATE_RE = re.compile(r"rate\s*=\s*(\d+)", re.IGNORECASE)
_DEFAULT_PCM_RATE = 24000


def _content_type_is_pcm(content_type: str) -> bool:
    """True when Content-Type declares raw PCM (e.g. Gemini audio/pcm; rate=24000)."""
    return "audio/pcm" in (content_type or "").lower()


def _content_type_is_wav(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "audio/wav" in ct or "audio/x-wav" in ct or "audio/wave" in ct


def _content_type_is_mp3(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "audio/mpeg" in ct or "audio/mp3" in ct


def _sniff_audio_format(audio_bytes: bytes) -> str | None:
    """Sniff audio container format from magic bytes."""
    if len(audio_bytes) >= 12 and audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        return "wav"
    if audio_bytes.startswith(b"ID3"):
        return "mp3"
    if len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and (audio_bytes[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def _pcm_rate_from_content_type(content_type: str) -> int:
    """Extract sample rate from Content-Type parameter, e.g. 'rate=24000'."""
    match = _PCM_RATE_RE.search(content_type or "")
    if match:
        try:
            val = int(match.group(1))
            if 8000 <= val <= 96000:
                return val
        except ValueError:
            pass
    return _DEFAULT_PCM_RATE


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono signed little-endian PCM samples in a standard WAV header."""
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm


def _alternate_tts_response_format(body: str, requested: str) -> str | None:
    """Suggest another response_format when the server rejected the requested one."""
    low = (body or "").lower()
    if requested == "mp3":
        if "pcm" in low:
            return "pcm"
        if "wav" in low:
            return "wav"
        if "unsupported response_format" in low or "invalid format" in low:
            return "wav"
    elif requested == "pcm":
        if "mp3" in low:
            return "mp3"
        if "wav" in low:
            return "wav"
        if "unsupported response_format" in low or "invalid format" in low:
            return "mp3"
    elif requested == "wav":
        if "mp3" in low:
            return "mp3"
        if "pcm" in low:
            return "pcm"
        if "unsupported response_format" in low or "invalid format" in low:
            return "mp3"
    return None


def _speech_failure_message(code: int, body: str) -> str:
    """User-visible line for Test voice / sidebar status. Includes the HTTP body."""
    snippet = " ".join((body or "").split())
    if len(snippet) > 300:
        snippet = snippet[:300] + "…"
    if code and snippet:
        return _("Speech request failed ({0}): {1}").format(code, snippet)
    if snippet:
        return _("Speech request failed: {0}").format(snippet)
    if code:
        return _("Speech request failed ({0}).").format(code)
    return _("Speech request failed.")


def _speech_read_timeout() -> float:
    """Settings read budget. Connect stays on the shared short timeout."""
    from plugin.framework.config import get_config_int_safe

    timeout = get_config_int_safe("request_timeout")
    if timeout <= 0:
        return 120.0
    return float(timeout)


def _post_audio_speech(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[bytes | None, str, int, str]:
    """Execute the HTTP request, returning (audio_bytes, content_type, code, error_body)."""
    import json

    from plugin.framework.client.http_transport import public_target
    from plugin.framework.client.requests import sync_request
    from plugin.framework.errors import NetworkError

    data = json.dumps(payload).encode("utf-8")
    try:
        result = sync_request(
            url,
            data=data,
            headers=headers,
            parse_json=False,
            method="POST",
            timeout=_speech_read_timeout(),
            stop_checker=stop_checker,
            include_meta=True,
        )
        return result.body, result.content_type, int(result.status or 200), ""
    except NetworkError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        code_raw = details.get("status")
        try:
            code = int(code_raw) if code_raw is not None else 0
        except (TypeError, ValueError):
            code = 0
        err_msg = str(exc)
        log.warning("TTS POST %s failed (%s): %s", public_target(url), code, err_msg)
        return None, "", code, err_msg


def _default_endpoint_format() -> str:
    """First response_format to ask for. Windows plays WAV with SoundPlayer."""
    return "wav" if sys.platform == "win32" else "mp3"


def _download_endpoint_speech(
    text: str,
    endpoint_url: str,
    api_key: str,
    model: str,
    voice: str,
    speed: float = 1.0,
    generation: int | None = None,
    on_status: Callable[[str], None] | None = None,
) -> str | None:
    """Download one ``/audio/speech`` clip to a tracked temp file.

    Remote TTS does not use the warm Kokoro worker. The caller plays the file
    and then ``_release_temp``. Returns None on failure or cancel.
    """
    from plugin.audio.tts_service import (
        _new_speech_temp,
        _playback_blocked,
        _release_temp,
    )
    from plugin.framework.client.model_fetcher import (
        cached_tts_response_format,
        remember_tts_response_format,
    )

    if _playback_blocked(generation):
        return None

    if "openrouter.ai" in str(endpoint_url or "").lower():
        from plugin.framework.client.model_fetcher import preferred_openrouter_tts_model_id

        model = preferred_openrouter_tts_model_id(model)

    url = endpoint_url.rstrip("/")
    if not url.endswith("/audio/speech"):
        if url.endswith("/v1"):
            url = f"{url}/audio/speech"
        else:
            url = f"{url}/v1/audio/speech"

    eff_voice = _resolve_tts_voice(model, voice)
    response_format = cached_tts_response_format(model)
    if response_format not in ("mp3", "pcm", "wav"):
        response_format = _default_endpoint_format()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "WriterAgent/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    audio_bytes: bytes | None = None
    content_type = ""
    code = 0
    err_body = ""

    for attempt in range(2):
        payload = {
            "model": model or "hexgrad/Kokoro-82M",
            "input": text,
            "voice": eff_voice,
            "speed": speed,
            "response_format": response_format,
        }
        log.info(
            "Requesting TTS from %s (model=%s, voice=%s, format=%s, text_len=%d)",
            url, payload["model"], eff_voice, response_format, len(text),
        )
        audio_bytes, content_type, code, err_body = _post_audio_speech(
            url, headers, payload, stop_checker=lambda: _playback_blocked(generation)
        )
        if _playback_blocked(generation):
            return None
        if err_body and attempt == 0:
            alt = _alternate_tts_response_format(err_body, response_format)
            if alt and alt != response_format:
                log.info(
                    "TTS response_format %s rejected for %s; retrying %s",
                    response_format, model, alt,
                )
                response_format = alt
                if _playback_blocked(generation):
                    return None
                continue
        break

    if err_body or audio_bytes is None:
        _notify_tts_status(_speech_failure_message(code, err_body), on_status)
        return None

    # Trust response content_type and magic bytes to determine format and whether to wrap PCM.
    sniffed = _sniff_audio_format(audio_bytes)
    actual_format: str
    if _content_type_is_pcm(content_type) and sniffed not in ("mp3", "wav"):
        actual_format = "pcm"
    elif _content_type_is_wav(content_type) or sniffed == "wav":
        actual_format = "wav"
    elif _content_type_is_mp3(content_type) or sniffed == "mp3":
        actual_format = "mp3"
    elif response_format in ("pcm", "wav", "mp3"):
        actual_format = response_format
    else:
        actual_format = "mp3"

    if err_body:
        remember_tts_response_format(model, response_format)
    elif _content_type_is_pcm(content_type) and actual_format == "pcm":
        remember_tts_response_format(model, "pcm")
    elif _content_type_is_wav(content_type) and actual_format == "wav":
        remember_tts_response_format(model, "wav")

    if actual_format == "pcm":
        rate = _pcm_rate_from_content_type(content_type)
        audio_bytes = _pcm_s16le_to_wav(audio_bytes, rate)
        suffix = ".wav"
    elif actual_format == "wav":
        suffix = ".wav"
    else:
        suffix = ".mp3"

    log.info("TTS audio received from %s (%d bytes, %s)", url, len(audio_bytes), suffix)
    if _playback_blocked(generation):
        log.info("TTS playback cancelled after download")
        return None

    tmp_file = _new_speech_temp(suffix, generation)
    if tmp_file is None:
        return None
    try:
        with open(tmp_file, "wb") as handle:
            handle.write(audio_bytes)
    except Exception:
        log.exception("Could not write TTS audio to %s", tmp_file)
        _release_temp(tmp_file)
        return None
    return tmp_file


def _speak_endpoint(
    text: str,
    endpoint_url: str,
    api_key: str,
    model: str = "",
    voice: str = "",
    speed: float = 1.0,
    generation: int | None = None,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Download audio from an OpenAI-compatible speech endpoint and play it."""
    from plugin.audio.tts_service import (
        _play_audio_file,
        _playback_blocked,
        _release_temp,
        _speak_system,
    )

    log.info("Speaking via endpoint TTS (%s, model=%s, voice=%s)", public_target(endpoint_url), model, voice)
    if _playback_blocked(generation):
        return

    wav_path = _download_endpoint_speech(
        text, endpoint_url, api_key, model, voice, speed=speed, generation=generation,
        on_status=on_status,
    )
    if wav_path:
        try:
            _play_audio_file(wav_path, generation=generation)
        finally:
            _release_temp(wav_path)
        return

    if _playback_blocked(generation):
        return

    log.warning("Endpoint TTS download failed; falling back to OS system speech.")
    _speak_system(text, speed=speed, generation=generation)
