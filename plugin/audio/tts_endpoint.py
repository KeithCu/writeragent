# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Endpoint HTTP text-to-speech synthesis (/v1/audio/speech)."""

from __future__ import annotations

import dataclasses
import logging
import re
import struct
import sys
import time
import urllib.parse
from typing import Any, Callable

from plugin.audio.tts_models import _notify_tts_status
from plugin.audio.tts_voices import canonical_voice_id, clean_voice_name, get_voice_family
from plugin.audio.voice_catalog import KOKORO_FALLBACK_VOICE
from plugin.framework.client.http_security import public_target
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

_PCM_RATE_RE = re.compile(r"rate\s*=\s*(\d+)", re.IGNORECASE)
_DEFAULT_PCM_RATE = 24000
_SPEECH_RESPONSE_MAX_BYTES = 32 * 1024 * 1024
_TTS_FORMAT_WORDS_RE = re.compile(r"\b(mp3|pcm|wav)\b")


@dataclasses.dataclass(frozen=True)
class _EndpointClip:
    audio: bytes
    suffix: str
    request_format: str


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
        val = int(match.group(1))
        if 8000 <= val <= 96000:
            return val
    return _DEFAULT_PCM_RATE


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono signed little-endian PCM samples in a standard WAV header."""
    data_size = len(pcm)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,  # block align
        16,
        b"data",
        data_size,
    ) + pcm


def _alternate_tts_response_format(body: str, requested: str, status: int) -> str | None:
    """Suggest another response_format when server returned 400, 415, or 422."""
    if status not in (400, 415, 422):
        return None
    low = (body or "").lower()
    for m in _TTS_FORMAT_WORDS_RE.finditer(low):
        fmt = m.group(1)
        if fmt != requested:
            return fmt
    return {"mp3": "wav", "wav": "mp3", "pcm": "mp3"}.get(requested)


def _speech_url(endpoint_url: str) -> str:
    """Normalize endpoint_url into an /audio/speech endpoint URL.

    Keeps query and userinfo on request URL, drops fragment, ensures path ends in /audio/speech.
    """
    raw = (endpoint_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/audio/speech"):
        new_path = path
    elif path.endswith("/v1"):
        new_path = f"{path}/audio/speech"
    else:
        new_path = f"{path}/v1/audio/speech"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ""))


def _explicit_openrouter(endpoint_url: str) -> bool:
    """True when is_openrouter config flag matches the scheme, host, port, and path."""
    from plugin.framework.errors import ConfigError

    try:
        from plugin.framework.config import get_config, get_current_endpoint

        flag = bool(get_config("is_openrouter"))
        curr = get_current_endpoint() or ""
    except ConfigError:
        return False
    if not flag or not curr:
        return False
    try:
        u1 = urllib.parse.urlsplit(curr.strip())
        u2 = urllib.parse.urlsplit(endpoint_url.strip())
    except ValueError:
        return False
    if u1.scheme.lower() != u2.scheme.lower():
        return False
    if (u1.hostname or "").lower() != (u2.hostname or "").lower():
        return False
    if u1.port != u2.port:
        return False
    p1 = (u1.path or "").rstrip("/")
    p2 = (u2.path or "").rstrip("/")
    return p1 == p2


def _speech_headers(endpoint_url: str, api_key: str) -> dict[str, str]:
    """Build headers for TTS POST, applying provider detection and auth config."""
    headers = {"Content-Type": "application/json"}
    mini = {
        "endpoint": endpoint_url,
        "api_key": api_key,
        "is_openrouter": _explicit_openrouter(endpoint_url),
    }
    from plugin.framework.client.auth import AuthError, build_auth_headers, resolve_auth_for_config
    from plugin.framework.errors import ConfigError

    try:
        resolved = resolve_auth_for_config(mini)
        headers.update(build_auth_headers(resolved))
    except (AuthError, ConfigError):
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _tts_format_key(endpoint_url: str, model: str) -> str:
    """Cache key for response_format, scoped to origin and model."""
    ep = (endpoint_url or "").strip()
    m = (model or "").strip()
    try:
        parsed = urllib.parse.urlsplit(ep)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if parsed.port is not None:
            origin = f"{scheme}://{host}:{parsed.port}"
        elif host:
            origin = f"{scheme}://{host}"
        else:
            origin = ep
    except ValueError:
        origin = ep
    return f"{origin}\n{m}"


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
            max_bytes=_SPEECH_RESPONSE_MAX_BYTES,
        )
        return result.body, result.content_type, int(result.status or 200), ""
    except NetworkError as exc:
        code_raw = exc.details.get("status")
        try:
            code = int(code_raw) if code_raw is not None else 0
        except (TypeError, ValueError):
            code = 0
        err_msg = str(exc)
        if exc.code == "STOPPED":
            # Stop is code="STOPPED"; the transport logs it at debug, and a warning here reads as a failed POST.
            log.debug("TTS POST %s aborted by Stop", public_target(url))
        else:
            log.warning("TTS POST %s failed (%s): %s", public_target(url), code, err_msg)
        return None, "", code, err_msg


def _default_endpoint_format() -> str:
    """First response_format to ask for. Windows plays WAV with SoundPlayer."""
    return "wav" if sys.platform == "win32" else "mp3"


def _resolve_tts_voice(model: str, voice: str, endpoint_url: str | None = None) -> str:
    """Ensure voice name is compatible with the target model."""
    voice = clean_voice_name(voice)
    m_low = (model or "").lower()
    is_kokoro = "kokoro" in m_low

    if not voice:
        if is_kokoro:
            return KOKORO_FALLBACK_VOICE
        fam = get_voice_family("endpoint", model, endpoint_url)
        if fam in ("openrouter", "together"):
            return ""
        return "alloy"

    if is_kokoro:
        family = "kokoro"
    elif endpoint_url is not None:
        family = get_voice_family("endpoint", model, endpoint_url)
    else:
        family = "openai"

    return canonical_voice_id(family, voice)


def _download_endpoint_speech(  # pyright: ignore[reportUnusedFunction]
    text: str,
    endpoint_url: str,
    api_key: str,
    model: str,
    voice: str,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
    *,
    stop_checker: Callable[[], bool] | None = None,
) -> _EndpointClip | None:
    """Download one ``/audio/speech`` clip returning audio bytes and metadata.

    Remote TTS does not use the warm Kokoro worker. Returns None on failure or cancel.
    """
    from plugin.framework.client.model_fetcher import (
        cached_tts_response_format,
        remember_tts_response_format,
    )

    if stop_checker and stop_checker():
        return None

    is_or = _explicit_openrouter(endpoint_url)
    from plugin.framework.client.provider_detection import is_openrouter_endpoint

    if is_openrouter_endpoint(endpoint_url, explicit_is_openrouter=is_or):
        from plugin.framework.client.model_fetcher import preferred_openrouter_tts_model_id

        model = preferred_openrouter_tts_model_id(model)

    url = _speech_url(endpoint_url)

    eff_voice = _resolve_tts_voice(model, voice, endpoint_url=endpoint_url)
    fmt_key = _tts_format_key(endpoint_url, model)
    response_format = cached_tts_response_format(fmt_key)
    if response_format not in ("mp3", "pcm", "wav"):
        response_format = _default_endpoint_format()

    headers = _speech_headers(endpoint_url, api_key)

    audio_bytes: bytes | None = None
    content_type = ""
    code = 0
    err_body = ""

    t0 = time.monotonic()
    for attempt in range(2):
        payload: dict[str, Any] = {
            "model": model or "hexgrad/Kokoro-82M",
            "input": text,
            "speed": speed,
            "response_format": response_format,
        }
        if eff_voice:
            payload["voice"] = eff_voice
        log.info(
            "Requesting TTS from %s (model=%s, voice=%s, format=%s, text_len=%d)",
            public_target(url), payload["model"], payload.get("voice", ""), response_format, len(text),
        )
        audio_bytes, content_type, code, err_body = _post_audio_speech(
            url, headers, payload, stop_checker=stop_checker,
        )
        if stop_checker and stop_checker():
            return None
        if err_body and attempt == 0:
            alt = _alternate_tts_response_format(err_body, response_format, code)
            if alt:
                log.info(
                    "TTS response_format %s rejected for %s; retrying %s",
                    response_format, model, alt,
                )
                response_format = alt
                if stop_checker and stop_checker():
                    return None
                continue
        break

    if err_body or audio_bytes is None:
        _notify_tts_status(_speech_failure_message(code, err_body), on_status)
        return None

    if not audio_bytes:
        log.warning("TTS endpoint %s returned empty audio body", public_target(url))
        _notify_tts_status(_("Speech endpoint returned no audio."), on_status)
        return None

    # Sniff magic bytes first; then match Content-Type; fallback to requested format.
    ct = (content_type or "").lower()
    sniffed = _sniff_audio_format(audio_bytes)
    if sniffed in ("wav", "mp3"):
        actual_format = sniffed
    elif any(token in ct for token in ("audio/pcm", "audio/x-pcm", "audio/l16", "codec=pcm")):
        actual_format = "pcm"
    elif any(token in ct for token in ("audio/wav", "audio/x-wav", "audio/wave")):
        actual_format = "wav"
    elif "audio/mpeg" in ct or "audio/mp3" in ct:
        actual_format = "mp3"
    else:
        actual_format = None
    if actual_format is None:
        actual_format = response_format
    else:
        remember_tts_response_format(fmt_key, actual_format)
    if actual_format == "pcm":
        audio_bytes = _pcm_s16le_to_wav(audio_bytes, _pcm_rate_from_content_type(content_type))
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "TTS audio received from %s (model=%s, format=%s, bytes=%d, elapsed_ms=%d)",
        public_target(url), model, actual_format, len(audio_bytes), elapsed_ms,
    )
    if stop_checker and stop_checker():
        log.info("TTS playback cancelled after download")
        return None
    suffix = ".mp3" if actual_format == "mp3" else ".wav"
    return _EndpointClip(audio=audio_bytes, suffix=suffix, request_format=actual_format)

