# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text-to-Speech (TTS) synthesis and playback service for WriterAgent."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable

from plugin.framework.config import get_config, get_api_key_for_endpoint
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

_active_speech_proc: subprocess.Popen[Any] | None = None
_speech_active: bool = False
_speech_cancelled = threading.Event()
_speech_lock = threading.Lock()


def is_speaking() -> bool:
    """Return True if TTS speech synthesis or playback is actively occurring."""
    with _speech_lock:
        return _speech_active or (_active_speech_proc is not None and _active_speech_proc.poll() is None)


def clean_text_for_speech(text: str) -> str:
    """Prepare text for spoken synthesis by stripping markdown, code, and noise.
    
    Removes fenced code blocks, raw URLs, excessive markup, and normalizes
    whitespace so speech output sounds natural and fluent.
    """
    if not text:
        return ""

    # Remove code blocks ```...```
    cleaned = re.sub(r"```[\s\S]*?```", " [code block omitted] ", text)

    # Remove inline code `...`
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)

    # Remove markdown images ![alt](url)
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", cleaned)

    # Replace markdown links [text](url) with just text
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)

    # Remove markdown headers #, ##, etc.
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)

    # Remove bold/italic markers
    cleaned = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", cleaned)

    # Remove HTML/XML tags
    cleaned = re.sub(r"<[^>]+>", "", cleaned)

    # Remove raw URLs
    cleaned = re.sub(r"https?://\S+", "link", cleaned)

    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def stop_speech() -> None:
    """Immediately stop and cancel any active speech synthesis or playback."""
    global _active_speech_proc, _speech_active
    with _speech_lock:
        was_active = _speech_active or _active_speech_proc is not None
        _speech_active = False
        if was_active:
            _speech_cancelled.set()
        else:
            _speech_cancelled.clear()
        if _active_speech_proc is not None:
            try:
                log.info("Terminating active speech playback process (PID %s)", _active_speech_proc.pid)
                _active_speech_proc.terminate()
                _active_speech_proc.poll()
            except Exception as e:
                log.debug("stop_speech terminate error: %s", e)
            finally:
                _active_speech_proc = None


_KOKORO_VOICES = {
    "alloy": "af_bella",
    "nova": "af_bella",
    "shimmer": "af_sarah",
    "echo": "am_adam",
    "fable": "am_michael",
    "onyx": "am_adam",
}


def _resolve_tts_voice(model: str, voice: str) -> str:
    """Ensure voice name is compatible with the target model."""
    if not voice:
        voice = "alloy"
    if "kokoro" in model.lower():
        v_low = voice.lower()
        if re.match(r"^[abefhjz][fm]_", v_low):
            return voice
        return _KOKORO_VOICES.get(v_low, "af_bella")
    return voice




def _play_audio_file(file_path: str) -> None:
    """Play an audio file using available OS command-line utilities."""
    global _active_speech_proc
    cmd: list[str] | None = None

    if sys.platform == "darwin":
        cmd = ["afplay", file_path]
    elif sys.platform == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f'(New-Object Media.SoundPlayer "{file_path}").PlaySync()',
        ]
    else:
        # Linux / Unix: prioritize players supporting MP3/WAV out-of-the-box
        if shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--no-video", file_path]
        elif shutil.which("mpg123"):
            cmd = ["mpg123", "-q", file_path]
        elif shutil.which("pw-play"):
            cmd = ["pw-play", file_path]
        elif shutil.which("paplay"):
            cmd = ["paplay", file_path]
        elif shutil.which("aplay"):
            cmd = ["aplay", file_path]

    if not cmd:
        log.warning("No audio player found on system to play: %s", file_path)
        return

    try:
        log.info("Playing audio with command: %s", " ".join(cmd))
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                log.info("Audio playback cancelled before process spawn")
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _active_speech_proc.wait()
        log.info("Audio playback completed successfully")
    except Exception as e:
        log.warning("_play_audio_file playback error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None


def _speak_system(text: str, speed: float = 1.0) -> None:
    """Speak text using built-in OS speech synthesis utilities."""
    global _active_speech_proc
    cmd: list[str] | None = None

    if sys.platform == "darwin":
        # macOS native say command
        rate = int(175 * speed)
        cmd = ["/usr/bin/say", "-r", str(rate), text]
    elif sys.platform == "win32":
        # Windows SAPI via PowerShell
        # Rate is integer from -10 to 10
        rate_int = int((speed - 1.0) * 5)
        rate_int = max(-10, min(10, rate_int))
        escaped = text.replace('"', '`"').replace("'", "''")
        ps_script = (
            f"Add-Type -AssemblyName System.Speech; "
            f"$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$synth.Rate = {rate_int}; "
            f"$synth.Speak('{escaped}')"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    else:
        # Linux native
        if shutil.which("spd-say"):
            rate_pct = int((speed - 1.0) * 100)
            rate_pct = max(-100, min(100, rate_pct))
            cmd = ["spd-say", "-r", str(rate_pct), "-w", text]
        elif shutil.which("espeak"):
            speed_wpm = int(160 * speed)
            cmd = ["espeak", "-s", str(speed_wpm), text]

    if not cmd:
        log.warning("No OS native text-to-speech utility (say/spd-say/espeak) found on system.")
        return

    try:
        log.info("Speaking via system command: %s", " ".join(cmd[:3]))
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                log.info("System speech cancelled before process spawn")
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _active_speech_proc.wait()
        log.info("System speech playback completed")
    except Exception as e:
        log.debug("_speak_system error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None


def _speak_endpoint(text: str, endpoint_url: str, api_key: str, model: str, voice: str, speed: float = 1.0) -> None:
    """Request speech audio from an OpenAI-compatible /audio/speech endpoint."""
    import urllib.request
    import urllib.error
    import json

    # Normalize endpoint URL to /audio/speech
    url = endpoint_url.rstrip("/")
    if not url.endswith("/audio/speech"):
        if url.endswith("/v1"):
            url = f"{url}/audio/speech"
        else:
            url = f"{url}/v1/audio/speech"

    eff_voice = _resolve_tts_voice(model, voice)
    payload = {
        "model": model or "hexgrad/Kokoro-82M",
        "input": text,
        "voice": eff_voice,
        "speed": speed,
        "response_format": "mp3",
    }

    log.info("Requesting TTS from %s (model=%s, voice=%s, text_len=%d)", url, payload["model"], eff_voice, len(text))

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "WriterAgent/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    tmp_file = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()

        log.info("TTS audio received from %s (%d bytes)", url, len(audio_bytes))

        if _speech_active and _speech_cancelled.is_set():
            log.info("TTS playback cancelled after download")
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp_file = f.name

        _play_audio_file(tmp_file)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        log.error("TTS HTTP error %d from %s: %s | Response: %s", getattr(e, "code", 0), url, e, err_body)
    except Exception as e:
        log.exception("TTS error from %s: %s", url, e)
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass


def speak_text_async(text: str, on_complete: Callable[[], None] | None = None) -> None:
    """Synthesize and speak text in a background thread."""
    global _speech_active
    if not bool(get_config("audio.tts_enabled")):
        log.debug("speak_text_async: TTS is disabled (audio.tts_enabled=False)")
        return

    clean = clean_text_for_speech(text)
    if not clean:
        log.debug("speak_text_async: No speakable text after cleaning")
        return

    with _speech_lock:
        _speech_active = True
        _speech_cancelled.clear()

    log.info("speak_text_async: queued speech for %d chars", len(clean))

    def _worker() -> None:
        global _speech_active
        try:
            if _speech_cancelled.is_set():
                return

            provider = str(get_config("audio.tts_provider") or "system").strip().lower()
            speed = float(get_config("audio.tts_speed") or 1.0)
            voice = str(get_config("audio.tts_voice") or "alloy").strip()

            log.info("TTS worker executing: provider=%s, speed=%.2f, voice=%s", provider, speed, voice)

            if provider == "system":
                _speak_system(clean, speed=speed)
            elif provider == "endpoint":
                from plugin.framework.config import get_current_endpoint
                from plugin.framework.client.model_fetcher import get_tts_model
                url = get_current_endpoint()
                api_key = get_api_key_for_endpoint(url)
                model = get_tts_model() or "hexgrad/Kokoro-82M"

                log.info("TTS endpoint resolved: url=%s, model=%s, has_key=%s", url, model, bool(api_key))

                if url:
                    _speak_endpoint(clean, url, api_key, model=model, voice=voice, speed=speed)
                else:
                    log.warning("No endpoint URL available for TTS; falling back to OS system speech.")
                    _speak_system(clean, speed=speed)
            else:
                _speak_system(clean, speed=speed)
        except Exception as e:
            log.exception("speak_text_async worker error: %s", e)
        finally:
            with _speech_lock:
                _speech_active = False
                _speech_cancelled.clear()
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

    run_in_background(_worker, dedicated=True)
