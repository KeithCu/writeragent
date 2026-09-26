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

from plugin.audio.kokoro_g2p import (
    KOKORO_ONNX_SCRIPT,
    KOKORO_PIP_INSTALL,
    ensure_kokoro_misaki,
    kokoro_lang_uses_misaki,
)
from plugin.audio.voice_catalog import (
    DEFAULT_VOICE_FOR_FAMILY,
    KOKORO_CATALOG_ITEMS as _KOKORO_CATALOG_ITEMS,
    KOKORO_FALLBACK_VOICE as _KOKORO_FALLBACK_VOICE,
    KOKORO_OPENAI_ALIASES as _KOKORO_VOICES,
    LOCALE_TO_KOKORO_DEFAULT as _LOCALE_TO_KOKORO_DEFAULT,
    LOCALE_TO_PIPER_DEFAULT as _LOCALE_TO_PIPER_DEFAULT,
    PIPER_FALLBACK_VOICE as _PIPER_FALLBACK_VOICE,
    PIPER_VOICE_MODELS as _PIPER_VOICE_MODELS,
    VOICE_CATALOGS,
    voice_short_name as _voice_short_name,
)
from plugin.framework.config import get_config, get_config_str, set_config, get_api_key_for_endpoint
from plugin.framework.i18n import _
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.sandbox import resolve_venv_python

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


# Voice lists, locale defaults, and OpenAI→Kokoro aliases load once from
# plugin/audio/data/voice_catalog.json (see voice_catalog.py).


def _kokoro_lang_for_voice(voice: str) -> str:
    """Determine the Kokoro phonemizer language code from voice prefix."""
    clean = voice.lower().strip()
    if clean.startswith("b"):
        return "en-gb"
    if clean.startswith("e"):
        return "es"
    if clean.startswith("f"):
        return "fr-fr"
    if clean.startswith("i"):
        return "it"
    if clean.startswith("j"):
        return "ja"
    if clean.startswith("z"):
        return "zh"
    if clean.startswith("h"):
        return "hi"
    if clean.startswith("p"):
        return "pt-br"
    return "en-us"


# UI language stem → Kokoro ``create(lang=)`` code. Kokoro has no German,
# Korean, and similar frontends; those locales stay on English espeak.
_UI_LOCALE_TO_KOKORO_LANG = {
    "en": "en-us",
    "es": "es",
    "fr": "fr-fr",
    "it": "it",
    "ja": "ja",
    "zh": "zh",
    "hi": "hi",
    "pt": "pt-br",
}

# English source for Settings → Speech → Test voice. ``_()`` speaks the UI locale.
TTS_TEST_SAMPLE = "Hello, I'm your LibreOffice WriterAgent."


def tts_test_sample() -> str:
    """Sample line for the Speech Test button, translated to the UI locale."""
    return _(TTS_TEST_SAMPLE)


def _kokoro_lang_for_script(text: str) -> str | None:
    """``ja`` when *text* has kana; ``zh`` when it has CJK ideographs and no kana."""
    has_kana = False
    has_han = False
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            has_kana = True
        elif 0x4E00 <= code <= 0x9FFF:
            has_han = True
    if has_kana:
        return "ja"
    if has_han:
        return "zh"
    return None


def kokoro_lang_for_ui_locale(locale: str | None = None, text: str = "") -> str:
    """Kokoro G2P code for the Settings Test sample.

    The sample is gettext'd to the LibreOffice UI locale, so that locale is
    the primary signal. Chat speech still uses :func:`_kokoro_lang_for_voice`.
    When the locale has no Kokoro language, a CJK script in *text* selects
    ``ja`` or ``zh`` so kanji is not handed to English espeak.

    TODO: real chat TTS should use langdetect (already installed in the dev
    venv) on the assistant text. English text on a non-English voice should
    then take the en-us G2P path. Keith will revisit that separately; do not
    call langdetect here.
    """
    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale

            locale = get_active_locale()
        except Exception:
            locale = "en_US"
    tag = (locale or "").split(".")[0].replace("-", "_")
    parts = [part for part in tag.split("_") if part]
    stem = parts[0].lower() if parts else ""
    region = parts[1].lower() if len(parts) > 1 else ""
    if stem == "en" and region == "gb":
        return "en-gb"
    mapped = _UI_LOCALE_TO_KOKORO_LANG.get(stem)
    if mapped:
        return mapped
    return _kokoro_lang_for_script(text) or "en-us"


def get_default_voice_for_locale(family: str, locale: str | None = None) -> str:
    """Return the optimal default voice for a voice family and locale."""
    fam = clean_provider_name(family)
    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale
            locale = get_active_locale()
        except Exception:
            locale = "en_US"
    stem = (locale or "").split(".")[0].split("_")[0].lower()
    if fam == "piper":
        return _LOCALE_TO_PIPER_DEFAULT.get(stem, _PIPER_FALLBACK_VOICE)
    if fam == "kokoro":
        return _LOCALE_TO_KOKORO_DEFAULT.get(stem, _KOKORO_FALLBACK_VOICE)
    if fam in ("openai", "endpoint"):
        return DEFAULT_VOICE_FOR_FAMILY["openai"]
    return DEFAULT_VOICE_FOR_FAMILY["system"]


def get_voice_catalog(family: str, locale: str | None = None) -> list[dict[str, str]]:
    """Return ordered voice options for the family, prioritizing the active locale."""
    fam = clean_provider_name(family)
    if fam not in ("piper", "kokoro"):
        return VOICE_CATALOGS.get(fam, [])

    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale
            locale = get_active_locale()
        except Exception:
            locale = "en_US"

    stem = (locale or "").split(".")[0].split("_")[0].lower()

    if fam == "kokoro":
        locale_voices: list[dict[str, str]] = []
        en_voices: list[dict[str, str]] = []
        other_voices: list[dict[str, str]] = []
        for opt in _KOKORO_CATALOG_ITEMS:
            v_lang = opt.get("lang", "en")
            item = {"value": opt["value"], "label": opt["label"]}
            if stem != "en" and v_lang == stem:
                locale_voices.append(item)
            elif v_lang == "en":
                en_voices.append(item)
            else:
                other_voices.append(item)
        return locale_voices + en_voices + other_voices

    # Piper: matching locale voices first, then English, then others
    locale_voices = []
    en_voices = []
    other_voices = []

    preferred_default = _LOCALE_TO_PIPER_DEFAULT.get(stem)

    for voice_id, model in _PIPER_VOICE_MODELS.items():
        lang_code = model[2]
        label = model[3]
        v_stem = lang_code.split("_")[0].lower()
        item = {"value": voice_id, "label": label}
        is_loc = stem != "en" and (
            voice_id == preferred_default
            or v_stem == stem
            or (stem in ("nb", "nn") and v_stem == "no")
            or (stem == "hr" and v_stem == "sl")
        )
        if is_loc:
            if voice_id == preferred_default:
                locale_voices.insert(0, item)
            else:
                locale_voices.append(item)
        elif v_stem == "en":
            en_voices.append(item)
        else:
            other_voices.append(item)

    return locale_voices + en_voices + other_voices


def settings_voice_options(services: Any = None) -> list[dict[str, str]]:
    """Locale-prioritized voice options for the provider saved in Settings.

    ``plugin/audio/module.yaml`` points ``tts_voice.options_provider`` here.
    The yaml ``options`` list is only a fallback stub for XDL generation and
    for when this call fails. ``services`` is unused; ``call_options_provider``
    always passes the service registry.
    """
    # call_options_provider always passes the registry; voice lists do not use it.
    del services
    provider = str(get_config("audio.tts_provider") or "system")
    model = ""
    if clean_provider_name(provider) == "endpoint":
        model = str(get_config("audio.tts_model") or "")
    return get_voice_catalog(get_voice_family(provider, model))


def clean_provider_name(provider_or_label: str) -> str:
    """Normalize provider name or UI label to clean provider code."""
    low = (provider_or_label or "").strip().lower()
    if "kokoro" in low:
        return "kokoro"
    if "piper" in low:
        return "piper"
    if "endpoint" in low:
        return "endpoint"
    return "system"


def clean_voice_name(voice_or_label: str) -> str:
    """Extract canonical voice code from a voice string or UI label."""
    if not voice_or_label:
        return ""
    return voice_or_label.split(" (")[0].strip()


def get_voice_family(provider: str | None, model: str | None = None) -> str:
    """Return voice family key ('kokoro', 'piper', 'openai', 'system')."""
    prov = clean_provider_name(provider or "")
    if prov == "kokoro":
        return "kokoro"
    if prov == "piper":
        return "piper"
    if prov == "endpoint":
        if model and "kokoro" in model.lower():
            return "kokoro"
        return "openai"
    return "system"


def get_scoped_tts_voice(
    provider: str | None = None,
    model: str | None = None,
    locale: str | None = None,
) -> str:
    """Get the scoped voice for the given provider/model's voice family."""
    if provider is None:
        provider = str(get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        try:
            from plugin.framework.client.model_fetcher import get_tts_model
            model = get_tts_model()
        except ImportError:
            model = None

    family = get_voice_family(prov_clean, model)
    scoped_key = f"audio.tts_voice_{family}"
    val = get_config(scoped_key)
    if val and isinstance(val, str) and val.strip():
        return clean_voice_name(val.strip())

    general_voice = str(get_config("audio.tts_voice") or "").strip()
    clean_gen = clean_voice_name(general_voice)
    valid_voices = {opt["value"] for opt in get_voice_catalog(family, locale)}
    if clean_gen in valid_voices:
        return clean_gen

    return get_default_voice_for_locale(family, locale)


def set_scoped_tts_voice(voice: str, provider: str | None = None, model: str | None = None) -> None:
    """Persist the voice selection for the given provider/model family."""
    clean_v = clean_voice_name(voice)
    if not clean_v:
        return
    if provider is None:
        provider = str(get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        try:
            from plugin.framework.client.model_fetcher import get_tts_model
            model = get_tts_model()
        except ImportError:
            model = None

    family = get_voice_family(prov_clean, model)
    scoped_key = f"audio.tts_voice_{family}"
    set_config(scoped_key, clean_v)
    set_config("audio.tts_voice", clean_v)


def parse_tts_speed(val: Any) -> float:
    """Parse speech speed safely, enforcing a minimum of 0.25x."""
    if val is None or val == "":
        return 1.0
    if isinstance(val, (int, float)):
        return max(0.25, min(5.0, float(val)))
    cleaned = str(val).split("(")[0].strip().rstrip("xX").strip().replace(",", ".")
    try:
        speed = float(cleaned)
        return max(0.25, min(5.0, speed))
    except (ValueError, TypeError):
        return 1.0


def _resolve_tts_voice(model: str, voice: str) -> str:
    """Ensure voice name is compatible with the target model."""
    voice = clean_voice_name(voice)
    if not voice:
        voice = "alloy"
    if "kokoro" in model.lower():
        v_low = voice.lower()
        if re.match(r"^[abefhjz][fm]_", v_low):
            return voice
        return _KOKORO_VOICES.get(v_low, _KOKORO_FALLBACK_VOICE)
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


def _notify_tts_status(message: str, on_status: Callable[[str], None] | None) -> None:
    """Log a TTS progress line and forward it when the caller supplied a callback.

    On-demand Piper/Kokoro downloads and the Lessac / OS-speech fallback used to
    be log-only. The callback is additive so the sidebar status field can show
    the same sentence. ``message`` is already translated.
    """
    log.info("%s", message)
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception:
        log.debug("TTS status callback failed", exc_info=True)


# The kokoro-onnx "model-files" release (kokoro-v0_19.onnx + voices.bin) is
# English-only. Settings lists multilingual ids such as jf_alpha that exist in
# voices-v1.0.bin; with the old pack the speak script substituted af_bella and
# still passed the requested lang, so Japanese and other languages sounded wrong.
# Default cache names are the v1.0 files from "model-files-v1.1" (the release
# the kokoro-onnx examples download). A cache that only has the old filenames
# misses these paths, so the next speak downloads the multilingual pack and
# leaves the English-only files in place. KOKORO_MODEL_PATH / KOKORO_VOICES_PATH
# still override both.
_KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
_KOKORO_VOICES_FILENAME = "voices-v1.0.bin"
_KOKORO_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
)


def _resolve_kokoro_model_files(on_status: Callable[[str], None] | None = None) -> tuple[str, str]:
    """Resolve paths to Kokoro ONNX model and voices file, downloading if missing."""
    cache_dir = os.path.expanduser("~/.cache/kokoro")
    default_model = os.path.join(cache_dir, _KOKORO_MODEL_FILENAME)
    default_voices = os.path.join(cache_dir, _KOKORO_VOICES_FILENAME)

    model_path = os.environ.get("KOKORO_MODEL_PATH") or default_model
    voices_path = os.environ.get("KOKORO_VOICES_PATH") or default_voices

    if os.path.exists(model_path) and os.path.exists(voices_path):
        return model_path, voices_path

    failed = False
    try:
        os.makedirs(cache_dir, exist_ok=True)
        import urllib.request
        needs_voices = not os.path.exists(voices_path) and voices_path == default_voices
        needs_model = not os.path.exists(model_path) and model_path == default_model
        if needs_voices or needs_model:
            _notify_tts_status(_("Downloading Kokoro voice model…"), on_status)
        if needs_voices:
            log.info("Downloading Kokoro voices to %s...", default_voices)
            urllib.request.urlretrieve(
                f"{_KOKORO_RELEASE_BASE}/{_KOKORO_VOICES_FILENAME}", default_voices
            )
            voices_path = default_voices
        if needs_model:
            log.info("Downloading Kokoro ONNX model to %s...", default_model)
            urllib.request.urlretrieve(
                f"{_KOKORO_RELEASE_BASE}/{_KOKORO_MODEL_FILENAME}", default_model
            )
            model_path = default_model
    except Exception as e:
        failed = True
        log.warning("Could not auto-download Kokoro models: %s", e)

    if failed:
        _notify_tts_status(_("Couldn't download Kokoro; using OS speech"), on_status)

    return model_path, voices_path


def _resolve_piper_model_file(voice: str, on_status: Callable[[str], None] | None = None) -> str:
    """Resolve path to Piper ONNX model file, downloading on demand if missing."""
    clean_v = clean_voice_name(voice)
    if not clean_v:
        clean_v = _PIPER_FALLBACK_VOICE

    if os.path.isabs(clean_v) and os.path.exists(clean_v):
        return clean_v

    cache_dir = os.path.expanduser("~/.cache/piper")
    voice_file = os.path.join(cache_dir, f"{clean_v}.onnx")
    json_file = os.path.join(cache_dir, f"{clean_v}.onnx.json")

    if os.path.exists(voice_file) and os.path.exists(json_file):
        return voice_file

    # If voice is in the curated catalog, download on demand.
    download_failed = False
    short = _voice_short_name(clean_v)
    if clean_v in _PIPER_VOICE_MODELS:
        rel_onnx, rel_json = _PIPER_VOICE_MODELS[clean_v][0], _PIPER_VOICE_MODELS[clean_v][1]
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        onnx_url = f"{base_url}/{rel_onnx}"
        json_url = f"{base_url}/{rel_json}"
        try:
            os.makedirs(cache_dir, exist_ok=True)
            import urllib.request
            _notify_tts_status(_("Downloading Piper voice {0}…").format(short), on_status)
            log.info("Downloading Piper voice model '%s' to %s...", clean_v, voice_file)
            req_onnx = urllib.request.Request(onnx_url, headers={"User-Agent": "WriterAgent/1.0"})
            with urllib.request.urlopen(req_onnx, timeout=60) as resp, open(voice_file, "wb") as f_out:
                shutil.copyfileobj(resp, f_out)
            req_json = urllib.request.Request(json_url, headers={"User-Agent": "WriterAgent/1.0"})
            with urllib.request.urlopen(req_json, timeout=30) as resp, open(json_file, "wb") as f_out:
                shutil.copyfileobj(resp, f_out)
            return voice_file
        except Exception as e:
            download_failed = True
            log.warning("Could not auto-download Piper voice '%s': %s", clean_v, e)
            if os.path.exists(voice_file):
                try:
                    os.remove(voice_file)
                except Exception:
                    pass
            if os.path.exists(json_file):
                try:
                    os.remove(json_file)
                except Exception:
                    pass

    # Fallback to the catalog's English default (Lessac) when the requested file is missing.
    default_voice_file = os.path.join(cache_dir, f"{_PIPER_FALLBACK_VOICE}.onnx")
    default_json = os.path.join(cache_dir, f"{_PIPER_FALLBACK_VOICE}.onnx.json")
    lessac_ready = os.path.exists(default_voice_file) and os.path.exists(default_json)
    other_voice = clean_v != _PIPER_FALLBACK_VOICE
    if lessac_ready:
        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status)
        return default_voice_file

    try:
        os.makedirs(cache_dir, exist_ok=True)
        import urllib.request
        rel_onnx, rel_json = _PIPER_VOICE_MODELS[_PIPER_FALLBACK_VOICE][0], _PIPER_VOICE_MODELS[_PIPER_FALLBACK_VOICE][1]
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        if download_failed and other_voice:
            _notify_tts_status(_("Couldn't download {0}; using Lessac").format(short), on_status)
        elif not download_failed:
            fallback_short = _voice_short_name(_PIPER_FALLBACK_VOICE)
            _notify_tts_status(_("Downloading Piper voice {0}…").format(fallback_short), on_status)
        log.info("Downloading Piper default voice model to %s...", default_voice_file)
        urllib.request.urlretrieve(f"{base_url}/{rel_onnx}", default_voice_file)
        urllib.request.urlretrieve(f"{base_url}/{rel_json}", default_json)
        return default_voice_file
    except Exception as e:
        log.warning("Could not auto-download Piper default voice model: %s", e)
        failed_name = short if download_failed else _voice_short_name(_PIPER_FALLBACK_VOICE)
        _notify_tts_status(_("Couldn't download {0}; using OS speech").format(failed_name), on_status)

    return voice


def _speak_kokoro_local(
    text: str,
    voice: str = _KOKORO_FALLBACK_VOICE,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
    lang: str | None = None,
) -> None:
    """Synthesize text using local Kokoro ONNX model in configured venv and play audio."""
    global _active_speech_proc
    log.info("Speaking via local Kokoro (voice=%s, speed=%.2f)", voice, speed)
    if _speech_active and _speech_cancelled.is_set():
        return

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        log.warning(
            "Local Kokoro TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed)
        return

    bin_dir = os.path.dirname(py_exe)
    cand_cli = os.path.join(bin_dir, "kokoro.exe" if sys.platform == "win32" else "kokoro")
    kokoro_cli: str | None = cand_cli if os.path.isfile(cand_cli) and os.access(cand_cli, os.X_OK) else None

    if kokoro_cli:
        tmp_wav = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_wav = f.name
            cmd = [kokoro_cli, "--voice", voice, "--speed", str(speed), "--output", tmp_wav, text]
            with _speech_lock:
                if _speech_active and _speech_cancelled.is_set():
                    return
                _active_speech_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            _active_speech_proc.wait()
            if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
                _play_audio_file(tmp_wav)
                return
        except Exception as e:
            log.warning("Kokoro CLI execution error: %s", e)
        finally:
            with _speech_lock:
                _active_speech_proc = None
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass

    model_path, voices_path = _resolve_kokoro_model_files(on_status=on_status)
    if not os.path.exists(model_path) or not os.path.exists(voices_path):
        log.warning("Kokoro ONNX model files missing (%s, %s); falling back to OS speech.", model_path, voices_path)
        _speak_system(text, speed=speed)
        return

    # Chat leaves lang empty and follows the voice id. The Settings Test
    # button passes the UI-locale code so the translated sample uses Misaki
    # (ja/zh/fr/…) instead of English espeak.
    if not lang or not str(lang).strip():
        lang = _kokoro_lang_for_voice(voice)
    else:
        lang = str(lang).strip().lower()
    # Non-English voices were phonemized with espeak-ng inside kokoro-onnx, so
    # ja read kanji as "chinese letter" and fr/es missed Kokoro's phone map.
    # Misaki runs in the venv script (is_phonemes=True). Install failure does
    # not refuse the speak: the script falls back to espeak-ng and the status
    # line carries the install hint. English stays on espeak-ng.
    misaki_ready: bool | None = True
    if kokoro_lang_uses_misaki(lang):
        misaki_ready = ensure_kokoro_misaki(
            py_exe,
            lang,
            on_status=lambda message: _notify_tts_status(message, on_status),
            cancelled=_speech_cancelled.is_set,
        )
        if misaki_ready is None or _speech_cancelled.is_set():
            return
    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        # KOKORO_ONNX_SCRIPT phonemizes non-English text with Misaki
        # (is_phonemes=True). A voices file that lacks the requested id still
        # speaks via af_bella; that substitution is logged and does not fail.
        cmd = [py_exe, "-c", KOKORO_ONNX_SCRIPT, text, voice, str(speed), tmp_wav, model_path, voices_path, lang]
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                return
            _active_speech_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
        _stdout, stderr = _active_speech_proc.communicate()
        if _active_speech_proc.returncode != 0:
            log.warning("Venv Kokoro failed (code %d): %s", _active_speech_proc.returncode, stderr)
        else:
            # A missing voice (custom KOKORO_VOICES_PATH still on the English
            # pack) and a Misaki import error are written to stderr while the
            # process still exits 0 and plays the fallback audio.
            stderr_text = (stderr or "").strip()
            if stderr_text:
                log.warning("Kokoro: %s", stderr_text)
            if misaki_ready and (
                "Misaki G2P failed" in stderr_text or "fell back to espeak-ng" in stderr_text
            ):
                _notify_tts_status(
                    _("Kokoro phonemizer failed; this language may be misread."),
                    on_status,
                )
            if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
                _play_audio_file(tmp_wav)
                return
    except Exception as e:
        log.warning("Venv Kokoro execution error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    log.warning(
        "Local Kokoro engine not available in configured venv. "
        "Install with: %s. Falling back to OS speech.",
        KOKORO_PIP_INSTALL,
    )
    _speak_system(text, speed=speed)


def _speak_piper_local(
    text: str,
    voice: str = _PIPER_FALLBACK_VOICE,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Synthesize text using local Piper fast neural TTS in configured venv and play audio."""
    global _active_speech_proc
    log.info("Speaking via local Piper (voice=%s, speed=%.2f)", voice, speed)
    if _speech_active and _speech_cancelled.is_set():
        return

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        log.warning(
            "Local Piper TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed)
        return

    model_file = _resolve_piper_model_file(voice, on_status=on_status)

    bin_dir = os.path.dirname(py_exe)
    cand_bin = os.path.join(bin_dir, "piper.exe" if sys.platform == "win32" else "piper")
    piper_bin: str | None = cand_bin if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK) else None

    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name

        length_scale = round(1.0 / max(0.2, min(5.0, speed)), 2)

        cmd: list[str] | None = None
        if piper_bin:
            cmd = [piper_bin, "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]
        else:
            cmd = [py_exe, "-m", "piper", "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]

        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        try:
            _, stderr = _active_speech_proc.communicate(input=text, timeout=30)
            if _active_speech_proc.returncode != 0:
                log.warning("Piper process failed (code %d): %s", _active_speech_proc.returncode, stderr)
        except Exception:
            _active_speech_proc.kill()
            raise

        if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            _play_audio_file(tmp_wav)
            return
        else:
            log.warning("Piper produced empty audio for voice %s; falling back to OS speech", voice)
    except FileNotFoundError:
        log.warning(
            "Local Piper executable not found in configured venv. Install via 'uv pip install piper-tts'. "
            "Falling back to OS speech."
        )
    except Exception as e:
        log.warning("Piper synthesis error: %s; falling back to OS speech", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    _speak_system(text, speed=speed)


def speak_text_async(
    text: str,
    on_complete: Callable[[], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    enabled: bool | None = None,
    lang: str | None = None,
) -> None:
    """Synthesize and speak text in a background thread.

    ``on_status`` receives short user-visible lines (model download, fallback).
    The chat panel posts those onto the sidebar status field.

    Keyword overrides are for Settings → Speech → Test voice, which speaks the
    controls on screen before OK writes them. Chat leaves them unset and reads
    ``audio.tts_*``. ``lang`` is a Kokoro G2P code; chat still derives that
    from the voice id inside :func:`_speak_kokoro_local`.
    """
    global _speech_active
    tts_on = bool(get_config("audio.tts_enabled")) if enabled is None else bool(enabled)
    if not tts_on:
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
    chosen_provider = provider
    chosen_model = model
    chosen_voice = voice
    chosen_speed = speed
    chosen_lang = lang

    def _worker() -> None:
        global _speech_active
        try:
            if _speech_cancelled.is_set():
                return

            raw_prov = chosen_provider if chosen_provider else str(get_config("audio.tts_provider") or "system")
            provider_code = clean_provider_name(raw_prov)
            speed_val = (
                parse_tts_speed(chosen_speed)
                if chosen_speed is not None
                else parse_tts_speed(get_config("audio.tts_speed"))
            )
            model_name = chosen_model.strip() if isinstance(chosen_model, str) else ""
            if provider_code == "endpoint" and not model_name:
                from plugin.framework.client.model_fetcher import get_tts_model
                model_name = get_tts_model() or "hexgrad/Kokoro-82M"

            if chosen_voice and chosen_voice.strip():
                voice_name = clean_voice_name(chosen_voice)
            else:
                voice_name = get_scoped_tts_voice(provider_code, model_name)

            log.info(
                "TTS worker executing: provider=%s, speed=%.2f, voice=%s, model=%s, lang=%s",
                provider_code,
                speed_val,
                voice_name,
                model_name,
                chosen_lang or "",
            )

            if provider_code == "system":
                _speak_system(clean, speed=speed_val)
            elif provider_code == "kokoro":
                if chosen_lang:
                    _speak_kokoro_local(
                        clean, voice=voice_name, speed=speed_val, on_status=on_status, lang=chosen_lang,
                    )
                else:
                    _speak_kokoro_local(clean, voice=voice_name, speed=speed_val, on_status=on_status)
            elif provider_code == "piper":
                _speak_piper_local(clean, voice=voice_name, speed=speed_val, on_status=on_status)
            elif provider_code == "endpoint":
                from plugin.framework.config import get_current_endpoint
                url = get_current_endpoint()
                api_key = get_api_key_for_endpoint(url)

                log.info("TTS endpoint resolved: url=%s, model=%s, has_key=%s", url, model_name, bool(api_key))

                if url:
                    _speak_endpoint(clean, url, api_key, model=model_name, voice=voice_name, speed=speed_val)
                else:
                    log.warning("No endpoint URL available for TTS; falling back to OS system speech.")
                    _speak_system(clean, speed=speed_val)
            else:
                _speak_system(clean, speed=speed_val)
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
