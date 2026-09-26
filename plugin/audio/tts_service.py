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
import struct
import subprocess
import sys
import tempfile
import threading
from collections import deque
from typing import Any, Callable, ClassVar

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

# Playback and one-shot synthesis overlap during sentence prefetch, so they
# cannot share one Popen slot. Stop kills both.
_play_proc: subprocess.Popen[Any] | None = None
_synth_procs: list[subprocess.Popen[Any]] = []
_speech_active: bool = False
_speech_cancelled = threading.Event()
_speech_lock = threading.Lock()
# Bumped on every stop and every new speak. In-flight work captured the old
# id and must not play or enqueue after that.
_speech_generation: int = 0
_tracked_temps: set[str] = set()
_ready_queue: "_ReadyQueue | None" = None
# generation -> {g2p lang: install succeeded}. One probe per language per
# reply, not once per sentence. A cancelled install is not stored.
_reply_misaki_ready: dict[int, dict[str, bool]] = {}

# Six clips is enough that a one-word sentence can be playing while the next
# long sentence (and a few after it) are already synthesized. A queue of one
# would wait to start sentence N+1 until N finishes playing — the gap this
# pipeline exists to avoid. The byte cap stops Stop mid-essay from leaving a
# large wav backlog; the second clip is always accepted so a short→long pair
# cannot stall on disk.
SPEECH_READY_MAX_CLIPS = 6
SPEECH_READY_MAX_BYTES = 32 * 1024 * 1024


class _ReadyClip:
    """One synthesized sentence waiting to play, in utterance order."""

    __slots__: ClassVar[tuple[str, ...]] = ("path", "text", "nbytes", "speak_system")
    path: str | None
    text: str
    nbytes: int
    speak_system: bool

    def __init__(self, path: str | None, text: str, nbytes: int, speak_system: bool) -> None:
        self.path = path
        self.text = text
        self.nbytes = nbytes
        self.speak_system = speak_system


class _ReadyQueue:
    """Bounded FIFO of clips. The producer blocks when the soft cap is hit."""

    _max_clips: int
    _max_bytes: int
    _bytes: int
    _cv: threading.Condition
    _closed: bool
    _drained: bool

    def __init__(self, max_clips: int, max_bytes: int) -> None:
        self._max_clips = max(2, max_clips)
        self._max_bytes = max_bytes
        self._items: deque[_ReadyClip] = deque()
        self._bytes = 0
        self._cv = threading.Condition()
        self._closed = False
        self._drained = False

    def _full_locked(self) -> bool:
        count = len(self._items)
        if count == 0:
            return False
        if count >= self._max_clips:
            return True
        # Always allow a second clip so "Hi." followed by a long sentence is
        # already synthesized before playback of "Hi." ends.
        if count >= 2 and self._bytes >= self._max_bytes:
            return True
        return False

    def put(self, clip: _ReadyClip, generation: int) -> bool:
        """Enqueue *clip*. False when this utterance was stopped."""
        with self._cv:
            while not self._drained and self._full_locked():
                if _playback_blocked(generation):
                    return False
                self._cv.wait(timeout=0.1)
            if self._drained or _playback_blocked(generation):
                return False
            self._items.append(clip)
            self._bytes += clip.nbytes
            self._cv.notify_all()
            return True

    def get(self, generation: int) -> _ReadyClip | None:
        """Next clip in order, or None when the utterance ended or was stopped."""
        with self._cv:
            while not self._items and not self._closed and not self._drained:
                if _playback_blocked(generation):
                    return None
                self._cv.wait(timeout=0.1)
            if self._drained or _playback_blocked(generation) or not self._items:
                return None
            clip = self._items.popleft()
            self._bytes -= clip.nbytes
            self._cv.notify_all()
            return clip

    def close(self) -> None:
        """Producer finished. The consumer drains what is already queued."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def drain(self) -> list[str]:
        """Drop every queued clip and return paths the caller must delete."""
        with self._cv:
            self._drained = True
            paths = [clip.path for clip in self._items if clip.path]
            self._items.clear()
            self._bytes = 0
            self._cv.notify_all()
            return paths


def _playback_blocked_locked(generation: int | None) -> bool:
    """True when *generation* must not start playback or another synth step.

    ``generation is None`` keeps the old direct-call behavior: helpers invoked
    outside ``speak_text_async`` still run after a previous Stop cleared
    ``_speech_active``. An in-flight utterance passes the id it captured.
    """
    if generation is None:
        return bool(_speech_active and _speech_cancelled.is_set())
    if generation != _speech_generation:
        return True
    return _speech_cancelled.is_set()


def _playback_blocked(generation: int | None) -> bool:
    with _speech_lock:
        return _playback_blocked_locked(generation)


def _proc_running(proc: subprocess.Popen[Any] | None) -> bool:
    return proc is not None and proc.poll() is None


def _terminate_proc(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    try:
        log.info("Terminating speech process (PID %s)", proc.pid)
        proc.terminate()
        proc.poll()
    except Exception as exc:
        log.debug("speech terminate error: %s", exc)


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        log.debug("Could not delete TTS temp %s", path, exc_info=True)


def is_speaking() -> bool:
    """Return True if TTS speech synthesis or playback is actively occurring."""
    with _speech_lock:
        if _speech_active or _proc_running(_play_proc):
            return True
        return any(_proc_running(proc) for proc in _synth_procs)


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
    """Stop playback, cancel in-flight synthesis, and delete clips not yet played.

    The generation id moves forward so a synth that finishes after Stop cannot
    enqueue or play. An idle warm Kokoro process is left running — Send calls
    this before the reply exists, and reloading ONNX on every turn would undo
    the keep-alive worker. A job that is actually inside ``Kokoro.create`` is
    killed, because that call cannot be interrupted any other way.
    """
    global _play_proc, _speech_active, _speech_generation, _ready_queue
    with _speech_lock:
        _speech_generation += 1
        was_active = _speech_active or _proc_running(_play_proc) or any(_proc_running(proc) for proc in _synth_procs)
        _speech_active = False
        if was_active:
            _speech_cancelled.set()
        else:
            _speech_cancelled.clear()
        play = _play_proc
        _play_proc = None
        synths = list(_synth_procs)
        _synth_procs.clear()
        queue_ref = _ready_queue
        _ready_queue = None
        temps = list(_tracked_temps)
        _tracked_temps.clear()
        _reply_misaki_ready.clear()
    _terminate_proc(play)
    for proc in synths:
        _terminate_proc(proc)
    try:
        from plugin.audio.kokoro_pool import cancel_kokoro_inflight

        cancel_kokoro_inflight()
    except Exception:
        log.debug("Kokoro cancel failed", exc_info=True)
    paths: list[str] = []
    if queue_ref is not None:
        paths.extend(queue_ref.drain())
    paths.extend(temps)
    for path in paths:
        _unlink_quiet(path)


def _begin_utterance() -> int:
    """Cancel whatever is speaking and return the generation id for the new one."""
    global _speech_active
    stop_speech()
    with _speech_lock:
        _speech_active = True
        _speech_cancelled.clear()
        return _speech_generation


def _end_utterance(generation: int) -> bool:
    """Drop the active flag when this utterance is still current.

    Returns True when the completion callback should run. Stop bumps the
    generation and clears ``_speech_active``, so the callback still runs and
    the sidebar can disable Stop. A newer ``speak_text_async`` sets active
    again; the older callback must not disable Stop out from under it.
    """
    global _speech_active
    with _speech_lock:
        if _speech_generation == generation:
            _speech_active = False
            _speech_cancelled.clear()
            _reply_misaki_ready.pop(generation, None)
            return True
        return not _speech_active


def _new_speech_temp(suffix: str, generation: int | None) -> str | None:
    """Create a temp file tracked so Stop can delete it if playback never starts."""
    if _playback_blocked(generation):
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            path = handle.name
    except Exception:
        log.warning("Could not create TTS temp file", exc_info=True)
        return None
    with _speech_lock:
        if _playback_blocked_locked(generation):
            _unlink_quiet(path)
            return None
        _tracked_temps.add(path)
    return path


def _release_temp(path: str | None) -> None:
    if not path:
        return
    with _speech_lock:
        _tracked_temps.discard(path)
    _unlink_quiet(path)


def _popen_for_speech(
    cmd: list[str],
    generation: int | None,
    *,
    slot: str,
    stdin: Any = None,
    stderr: Any = subprocess.DEVNULL,
    text: bool = False,
) -> subprocess.Popen[Any] | None:
    """Spawn a play or synth process and register it so ``stop_speech`` can kill it."""
    global _play_proc
    with _speech_lock:
        if _playback_blocked_locked(generation):
            log.info("Speech cancelled before process spawn")
            return None
        proc = subprocess.Popen(
            cmd,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            text=text,
        )
        if slot == "play":
            _play_proc = proc
        else:
            _synth_procs.append(proc)
        return proc


def _clear_speech_proc(proc: subprocess.Popen[Any] | None, slot: str) -> None:
    global _play_proc
    if proc is None:
        return
    with _speech_lock:
        if slot == "play":
            if _play_proc is proc:
                _play_proc = None
        else:
            try:
                _synth_procs.remove(proc)
            except ValueError:
                pass


def sentence_speak_enabled() -> bool:
    """True when assistant replies are spoken one sentence at a time.

    The schema default is on. A missing key (tests that stub ``get_config``,
    or a manifest generated before this setting existed) stays on so the
    prefetch path is what Speech uses unless the checkbox is cleared.
    """
    try:
        val = get_config("audio.tts_sentence_mode")
    except Exception:
        return True
    if val is None:
        return True
    from plugin.framework.config_schema import as_bool

    return as_bool(val)


def _speech_locale_key() -> str:
    """BCP-47 tag for the grammar sentence splitter (``en_US`` → ``en-US``)."""
    try:
        from plugin.framework.i18n import get_active_locale
        from plugin.writer.locale.grammar_proofread_locale import normalize_detected_bcp47

        raw = get_active_locale() or "en_US"
        return normalize_detected_bcp47(raw) or str(raw).replace("_", "-")
    except Exception:
        return "en-US"


def sentences_for_speech(text: str, ctx: Any) -> list[str]:
    """Split *text* with the grammar checker’s sentence splitter.

    Must run on the thread that owns *ctx* (the sidebar drain after
    SEND_COMPLETED). ``split_into_sentences`` calls LibreOffice’s
    BreakIterator (``com.sun.star.i18n.BreakIterator``); that service is not
    safe on the audio worker. The worker receives this list and does not
    touch UNO.

    ``filter_sentence_spans_for_thresholds`` is not applied. That helper drops
    short incomplete sentences to limit grammar churn; those fragments are
    still part of the reply and have to be spoken. ``looks_complete_sentence``
    is the same kind of gate and is left for a later mid-stream mode.
    """
    locale_key = _speech_locale_key()
    try:
        from plugin.writer.locale.grammar_proofread_text import (
            merge_dialogue_sentences,
            split_into_sentences,
        )

        pairs = merge_dialogue_sentences(split_into_sentences(ctx, locale_key, text))
    except Exception:
        log.exception("TTS sentence split failed; speaking the reply as one clip")
        stripped = text.strip()
        return [stripped] if stripped else []
    spoken: list[str] = []
    for _offset, chunk in pairs:
        piece = chunk.strip()
        if piece:
            spoken.append(piece)
    if spoken:
        return spoken
    stripped = text.strip()
    return [stripped] if stripped else []


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


# English source for Settings → Speech → Test voice. ``_()`` speaks the UI locale.
TTS_TEST_SAMPLE = "Hello, I'm your LibreOffice WriterAgent."

# Basic Latin through Latin Extended-B. Accented French/Spanish still counts
# as Latin; langdetect would be needed to split those from English.
_LATIN_LETTER_MAX = 0x024F


def tts_test_sample() -> str:
    """Sample line for the Speech Test button, translated to the UI locale."""
    return _(TTS_TEST_SAMPLE)


def _text_is_latin_script(text: str) -> bool:
    """True when every letter is Latin. Kana, Han, and Devanagari are not."""
    for ch in text:
        if ch.isalpha() and ord(ch) > _LATIN_LETTER_MAX:
            return False
    return True


def kokoro_g2p_lang(text: str, voice: str = "") -> str:
    """Phonemizer code for one Kokoro utterance. The voice id is not changed.

    English voices (``a*``, ``b*``) use kokoro-onnx's built-in espeak path
    (``en-us`` or ``en-gb``).

    Non-Latin frontends (``ja``, ``zh``, ``hi``) cannot process Latin text
    (e.g. an English UI sample or reply on an Asian voice). When the text is
    pure Latin script, they fall back to ``en-us`` so the voice can speak
    English without failing or garbling Misaki.

    Romance voices (``fr-fr``, ``es``, ``it``, ``pt-br``) keep their native
    Misaki language so native speech is not forced into English G2P.
    """
    if not voice:
        return "en-us"
    target = _kokoro_lang_for_voice(voice)
    if target in ("en-us", "en-gb"):
        return target
    if target in ("ja", "zh", "hi") and _text_is_latin_script(text):
        return "en-us"
    return target


def _normalize_voice_family(family: str) -> str:
    """Family key, or a provider label reduced to a family.

    ``clean_provider_name`` maps anything that is not Kokoro/Piper/endpoint to
    ``system``. Passing the family id ``openai`` through it used to do that, so
    the OpenAI catalog never reached the Voice combo.
    """
    fam = (family or "").strip().lower()
    if fam in ("piper", "kokoro", "openai", "system", "openrouter", "endpoint"):
        return fam
    return clean_provider_name(family)


def get_default_voice_for_locale(family: str, locale: str | None = None) -> str:
    """Return the optimal default voice for a voice family and locale."""
    fam = _normalize_voice_family(family)
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
    # OpenRouter speech models have no static default; the harvested list supplies one.
    if fam == "openrouter":
        return ""
    return DEFAULT_VOICE_FOR_FAMILY["system"]


def get_voice_catalog(family: str, locale: str | None = None) -> list[dict[str, str]]:
    """Return ordered voice options for the family, prioritizing the active locale."""
    fam = _normalize_voice_family(family)
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
    return voice_options_for_provider(provider, model)


def voice_options_for_provider(
    provider: str,
    model: str | None = None,
    locale: str | None = None,
) -> list[dict[str, str]]:
    """Voice rows for Settings.

    Endpoint models with a harvested ``supported_voices`` list use those ids.
    An OpenRouter speech model whose list omitted voices, or whose speech list
    has not been fetched yet, returns ``[]`` so the combo stays free text
    instead of the OpenAI alloy list. Other OpenAI-compatible endpoints keep
    the openai catalog. Local Kokoro and Piper are unchanged.
    """
    prov = clean_provider_name(provider)
    if prov == "endpoint":
        rows = _endpoint_voice_rows(str(model or ""))
        if rows is not None:
            return rows
    return get_voice_catalog(get_voice_family(prov, model), locale)


def _endpoint_voice_rows(model: str) -> list[dict[str, str]] | None:
    """OR voice rows, ``[]`` for free text, or None to use the family catalog."""
    from plugin.framework.client.model_fetcher import (
        cached_tts_supported_voices,
        openrouter_speech_list_has_model,
        openrouter_speech_list_loaded,
    )

    voices = cached_tts_supported_voices(model) if model else []
    if voices:
        # Label equals the API id. Pretty-casing would hide ids the request must send.
        return [{"value": voice, "label": voice} for voice in voices]
    if model and "kokoro" in model.lower():
        return None
    if model and openrouter_speech_list_has_model(model):
        # Fetched speech row with no supported_voices: do not invent alloy.
        return []
    if _saved_endpoint_is_openrouter() and not openrouter_speech_list_loaded():
        # Not fetched yet. Alloy is the wrong list for Grok/Gemini speech models.
        return []
    return None


def _saved_endpoint_is_openrouter() -> bool:
    """True when the saved chat endpoint is OpenRouter.

    Speech uses Current Chat Endpoint, so the host that will speak is the
    saved URL, not an unsaved value still sitting in the endpoint combo.
    """
    try:
        from plugin.framework.client.provider_detection import get_provider_from_endpoint
        from plugin.framework.config import get_current_endpoint

        return get_provider_from_endpoint(get_current_endpoint() or "") == "openrouter"
    except Exception:
        log.debug("TTS voice list: endpoint provider unavailable", exc_info=True)
        return False


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
    """Return voice family key ('kokoro', 'piper', 'openai', 'openrouter', 'system')."""
    prov = clean_provider_name(provider or "")
    if prov == "kokoro":
        return "kokoro"
    if prov == "piper":
        return "piper"
    if prov == "endpoint":
        # Kokoro on an endpoint still shares the local Kokoro voice key.
        if model and "kokoro" in model.lower():
            return "kokoro"
        if model and _endpoint_uses_openrouter_voices(model):
            return "openrouter"
        return "openai"
    return "system"


def _endpoint_uses_openrouter_voices(model: str) -> bool:
    """True when this endpoint model should not use the OpenAI voice family.

    Harvested ``supported_voices`` win. A speech-list id with no voice array
    is the same family so a typed voice is not stored as ``alloy``.
    """
    from plugin.framework.client.model_fetcher import (
        cached_tts_supported_voices,
        openrouter_speech_list_has_model,
    )

    if cached_tts_supported_voices(model):
        return True
    return openrouter_speech_list_has_model(model)


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
    or_voices: list[str] = []
    if prov_clean == "endpoint" and model:
        from plugin.framework.client.model_fetcher import cached_tts_supported_voices

        or_voices = cached_tts_supported_voices(model)

    scoped_key = f"audio.tts_voice_{family}"
    val = get_config(scoped_key)
    clean_scoped = clean_voice_name(val.strip()) if isinstance(val, str) and val.strip() else ""
    general_voice = str(get_config("audio.tts_voice") or "").strip()
    clean_gen = clean_voice_name(general_voice)

    # Model-specific OpenRouter ids beat a stored OpenAI voice such as alloy.
    if or_voices:
        if clean_scoped in or_voices:
            return clean_scoped
        if clean_gen in or_voices:
            return clean_gen
        return or_voices[0]

    if clean_scoped:
        return clean_scoped

    if family == "openrouter":
        # No advertised list: keep a typed id. Do not substitute alloy.
        return clean_gen

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




def _play_audio_file(file_path: str, generation: int | None = None) -> None:
    """Play an audio file using available OS command-line utilities."""
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

    proc: subprocess.Popen[Any] | None = None
    try:
        log.info("Playing audio with command: %s", " ".join(cmd))
        proc = _popen_for_speech(cmd, generation, slot="play")
        if proc is None:
            return
        proc.wait()
        log.info("Audio playback completed successfully")
    except Exception as e:
        log.warning("_play_audio_file playback error: %s", e)
    finally:
        _clear_speech_proc(proc, "play")


def _speak_system(text: str, speed: float = 1.0, generation: int | None = None) -> None:
    """Speak text using built-in OS speech synthesis utilities."""
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

    proc: subprocess.Popen[Any] | None = None
    try:
        log.info("Speaking via system command: %s", " ".join(cmd[:3]))
        proc = _popen_for_speech(cmd, generation, slot="play")
        if proc is None:
            return
        proc.wait()
        log.info("System speech playback completed")
    except Exception as e:
        log.debug("_speak_system error: %s", e)
    finally:
        _clear_speech_proc(proc, "play")


_PCM_RATE_RE = re.compile(r"rate\s*=\s*(\d+)", re.IGNORECASE)
_DEFAULT_PCM_RATE = 24000


def _response_content_type(resp: Any) -> str:
    """Content-Type from a urllib response, or empty when the mock has none."""
    headers = getattr(resp, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    try:
        value = getter("Content-Type")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _content_type_is_pcm(content_type: str) -> bool:
    low = (content_type or "").lower()
    return "audio/pcm" in low or "audio/l16" in low


def _content_type_is_wav(content_type: str) -> bool:
    low = (content_type or "").lower()
    return "audio/wav" in low or "audio/wave" in low or "audio/x-wav" in low


def _pcm_rate_from_content_type(content_type: str) -> int:
    """Sample rate from ``audio/pcm;rate=…``, else 24 kHz."""
    match = _PCM_RATE_RE.search(content_type or "")
    if not match:
        return _DEFAULT_PCM_RATE
    try:
        rate = int(match.group(1))
    except ValueError:
        return _DEFAULT_PCM_RATE
    return rate if rate > 0 else _DEFAULT_PCM_RATE


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw signed-16-bit little-endian mono PCM in a RIFF/WAVE header.

    Players invoked by ``_play_audio_file`` do not play a bare PCM blob. OpenRouter
    Gemini speech returns that blob (often ``audio/pcm;rate=24000``).
    """
    channels = 1
    bits_per_sample = 16
    block_align = channels * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    data_size = len(pcm)
    header = b"".join((
        b"RIFF",
        struct.pack("<I", 36 + data_size),
        b"WAVE",
        b"fmt ",
        struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample),
        b"data",
        struct.pack("<I", data_size),
    ))
    return header + pcm


def _alternate_tts_response_format(body: str, requested: str) -> str | None:
    """Format to retry once when the error says ``requested`` is not accepted.

    pcm wins when the body names it (Gemini: mp3 is rejected, pcm is required).
    wav is the retry when the body names wav and not pcm. A bare "mp3 is not
    supported" retries pcm, which is the OpenRouter speech miss we have seen.
    Unrelated errors (auth, unknown voice) do not match and are not retried.
    """
    low = (body or "").lower()
    talks_format = any(token in low for token in (
        "response_format",
        "response format",
        "audio format",
        "audio/pcm",
        "audio/l16",
        "audio/wav",
        "audio/mpeg",
    ))
    unsupported_requested = requested in low and any(
        token in low for token in ("not support", "unsupported", "invalid", "only support", "must be")
    )
    if not talks_format and not unsupported_requested:
        return None
    if "pcm" in low and requested != "pcm":
        return "pcm"
    if "wav" in low and requested != "wav":
        return "wav"
    if requested == "mp3" and unsupported_requested:
        return "pcm"
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


def _post_audio_speech(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[bytes | None, str, int, str]:
    """POST one speech clip.

    Returns ``(audio, content_type, http_code, error_body)``. ``error_body`` is
    empty on success. The body is the raw response text so format detection and
    the status line can both show it.
    """
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = getattr(resp, "status", None)
            http_code = code if isinstance(code, int) else 200
            return resp.read(), _response_content_type(resp), http_code, ""
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            raw = exc.read()
            if isinstance(raw, bytes):
                err_body = raw.decode("utf-8", errors="replace")
            elif isinstance(raw, str):
                err_body = raw
        except Exception:
            err_body = ""
        code = int(getattr(exc, "code", 0) or 0)
        if not err_body:
            err_body = str(exc)
        log.error("TTS HTTP error %d from %s: %s | Response: %s", code, url, exc, err_body)
        return None, "", code, err_body
    except Exception as exc:
        log.exception("TTS error from %s: %s", url, exc)
        return None, "", 0, str(exc)


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

    ``response_format`` starts at ``mp3`` unless this process already learned
    another format for the model (see ``remember_tts_response_format``). A
    pcm-only or wav-only error retries once. ``audio/pcm`` bytes are wrapped
    as WAV before the path is returned — the file used to be named ``.mp3``
    and players stayed silent.
    """
    from plugin.framework.client.model_fetcher import (
        cached_tts_response_format,
        remember_tts_response_format,
    )

    if _playback_blocked(generation):
        return None

    # Catalog Kokoro is hexgrad/Kokoro-82M; the OpenRouter speech list uses
    # hexgrad/kokoro-82m. Send the API id when that list is cached.
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
    response_format = cached_tts_response_format(model) or "mp3"
    if response_format not in ("mp3", "pcm", "wav"):
        response_format = "mp3"

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
    tried_alt = False
    while True:
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
        audio_bytes, content_type, code, err_body = _post_audio_speech(url, headers, payload)
        if err_body and not tried_alt:
            alt = _alternate_tts_response_format(err_body, response_format)
            if alt and alt != response_format:
                # One retry. The format that succeeds is remembered so the next
                # clip does not ask for mp3 again.
                log.info(
                    "TTS response_format %s rejected for %s; retrying %s",
                    response_format, model, alt,
                )
                response_format = alt
                tried_alt = True
                if _playback_blocked(generation):
                    return None
                continue
        break

    if err_body or audio_bytes is None:
        _notify_tts_status(_speech_failure_message(code, err_body), on_status)
        return None

    if response_format in ("pcm", "wav"):
        remember_tts_response_format(model, response_format)

    # Asked for mp3 and the server still returned raw PCM (or named pcm).
    # Remember that so the next request does not ask for mp3 again.
    if _content_type_is_pcm(content_type):
        remember_tts_response_format(model, "pcm")
        response_format = "pcm"
    elif _content_type_is_wav(content_type):
        remember_tts_response_format(model, "wav")
        response_format = "wav"

    if response_format == "pcm" or _content_type_is_pcm(content_type):
        rate = _pcm_rate_from_content_type(content_type)
        audio_bytes = _pcm_s16le_to_wav(audio_bytes, rate)
        suffix = ".wav"
    elif response_format == "wav":
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
    model: str,
    voice: str,
    speed: float = 1.0,
    generation: int | None = None,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Request speech audio from an OpenAI-compatible /audio/speech endpoint."""
    tmp_file = _download_endpoint_speech(
        text, endpoint_url, api_key, model, voice, speed=speed, generation=generation,
        on_status=on_status,
    )
    if not tmp_file:
        return
    try:
        _play_audio_file(tmp_file, generation=generation)
    finally:
        _release_temp(tmp_file)


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
# voices-v1.0.bin; with the old pack the speak script substituted af_sky and
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


def _notify_kokoro_g2p_fallback(
    detail: str,
    misaki_ready: bool | None,
    on_status: Callable[[str], None] | None,
) -> None:
    """Status line when Misaki was installed but this clip still used espeak-ng.

    ``misaki_ready`` is False when the install already failed: that path has
    its own message and must not add a second one. None means Stop.
    """
    if not misaki_ready:
        return
    if "Misaki G2P failed" not in detail and "fell back to espeak-ng" not in detail:
        return
    _notify_tts_status(
        _("Kokoro phonemizer failed; this language may be misread."),
        on_status,
    )


def _remember_misaki_ready(generation: int | None, lang: str, ready: bool) -> None:
    if generation is None:
        return
    with _speech_lock:
        _reply_misaki_ready.setdefault(generation, {})[lang] = ready


def _cached_misaki_ready(generation: int | None, lang: str) -> bool | None:
    """Ready flag already computed for this reply and lang, or None if new."""
    if generation is None:
        return None
    with _speech_lock:
        cached = _reply_misaki_ready.get(generation)
    if not cached or lang not in cached:
        return None
    return cached[lang]


def _prepare_kokoro_misaki(
    text: str,
    voice: str,
    on_status: Callable[[str], None] | None,
    generation: int | None,
    py_exe: str | None = None,
) -> bool | None:
    """Install Misaki at most once per G2P language for this reply.

    The language comes from :func:`kokoro_g2p_lang`: Latin text stays on
    English espeak even when the voice is ``jf_alpha``. True means the probe
    passed or this utterance does not use Misaki. False means install failed;
    synthesis still runs and may fall back to espeak-ng. None means Stop
    during install: the caller must not synthesize and must not fall through
    to OS speech.
    """
    lang = kokoro_g2p_lang(text, voice)
    cached = _cached_misaki_ready(generation, lang)
    if cached is not None:
        return cached
    if not kokoro_lang_uses_misaki(lang):
        _remember_misaki_ready(generation, lang, True)
        return True
    if py_exe is None:
        venv_dir = get_config_str("scripting.python_venv_path").strip()
        py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        _remember_misaki_ready(generation, lang, False)
        return False

    def _cancelled() -> bool:
        return _speech_cancelled.is_set() or _playback_blocked(generation)

    ready = ensure_kokoro_misaki(
        py_exe,
        lang,
        on_status=lambda message: _notify_tts_status(message, on_status),
        cancelled=_cancelled,
    )
    # None is "do not speak", including when Stop lands after a successful
    # probe. Do not cache that: the next reply has a new generation.
    if ready is None or _cancelled():
        return None
    _remember_misaki_ready(generation, lang, bool(ready))
    return bool(ready)


def _kokoro_warm_to_file(
    text: str,
    voice: str,
    speed: float,
    model_path: str,
    voices_path: str,
    generation: int | None,
    on_status: Callable[[str], None] | None = None,
    misaki_ready: bool | None = True,
) -> tuple[str | None, bool]:
    """Synthesize with the keep-alive worker.

    Returns ``(wav_path, allow_fallback)``. ``allow_fallback`` is False when
    the utterance was cancelled: starting the cold one-shot after Stop would
    speak text the user already dismissed. A spawn or import failure leaves
    ``allow_fallback`` True so the existing ``python -c`` path still runs.
    """
    if _playback_blocked(generation):
        return None, False
    try:
        from plugin.audio.kokoro_pool import get_kokoro_pool

        pool = get_kokoro_pool()
    except Exception:
        log.exception("Kokoro warm worker unavailable")
        return None, True
    if pool is None:
        return None, True
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None, False
    result = pool.execute(
        {
            "text": text,
            "voice": voice,
            "speed": speed,
            "lang": kokoro_g2p_lang(text, voice),
            "model_path": model_path,
            "voices_path": voices_path,
            "out_path": tmp_wav,
        }
    )
    if _playback_blocked(generation) or (isinstance(result, dict) and result.get("code") == "WORKER_CANCELLED"):
        _release_temp(tmp_wav)
        return None, False
    if (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and os.path.isfile(tmp_wav)
        and os.path.getsize(tmp_wav) > 0
    ):
        warning = result.get("warning") if isinstance(result, dict) else ""
        if isinstance(warning, str) and warning.strip():
            _notify_kokoro_g2p_fallback(warning, misaki_ready, on_status)
        return tmp_wav, False
    err = result.get("error") if isinstance(result, dict) else result
    log.warning("Warm Kokoro worker failed (%s); falling back to one-shot synthesis", err)
    _release_temp(tmp_wav)
    return None, True


def _kokoro_oneshot_to_file(
    py_exe: str,
    text: str,
    voice: str,
    speed: float,
    model_path: str,
    voices_path: str,
    generation: int | None,
    on_status: Callable[[str], None] | None = None,
    misaki_ready: bool | None = True,
) -> str | None:
    """Cold ``python -c`` Kokoro load. Used when the warm worker cannot start.

    ``KOKORO_ONNX_SCRIPT`` phonemizes non-English text with Misaki
    (``is_phonemes=True``). A voices file that lacks the requested id still
    speaks via ``af_sky``; that substitution is logged and does not fail.
    """
    if _playback_blocked(generation):
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    lang = kokoro_g2p_lang(text, voice)
    cmd = [py_exe, "-c", KOKORO_ONNX_SCRIPT, text, voice, str(speed), tmp_wav, model_path, voices_path, lang]
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _popen_for_speech(cmd, generation, slot="synth", stderr=subprocess.PIPE, text=True)
        if proc is None:
            _release_temp(tmp_wav)
            return None
        _unused_stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            log.warning("Venv Kokoro failed (code %d): %s", proc.returncode, stderr)
            _release_temp(tmp_wav)
            return None
        stderr_text = str(stderr or "").strip()
        if stderr_text:
            log.warning("Kokoro: %s", stderr_text)
            _notify_kokoro_g2p_fallback(stderr_text, misaki_ready, on_status)
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            return tmp_wav
    except Exception as exc:
        log.warning("Venv Kokoro execution error: %s", exc)
    finally:
        _clear_speech_proc(proc, "synth")
    _release_temp(tmp_wav)
    return None


def _kokoro_cli_to_file(
    kokoro_cli: str,
    text: str,
    voice: str,
    speed: float,
    generation: int | None,
) -> str | None:
    """Cold Kokoro CLI. Only used when the warm ONNX worker did not produce audio."""
    if _playback_blocked(generation):
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    cmd = [kokoro_cli, "--voice", voice, "--speed", str(speed), "--output", tmp_wav, text]
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _popen_for_speech(cmd, generation, slot="synth")
        if proc is None:
            _release_temp(tmp_wav)
            return None
        proc.wait()
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            return tmp_wav
    except Exception as exc:
        log.warning("Kokoro CLI execution error: %s", exc)
    finally:
        _clear_speech_proc(proc, "synth")
    _release_temp(tmp_wav)
    return None


def _kokoro_audio_file(
    text: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    generation: int | None,
    misaki_ready: bool | None = None,
) -> str | None:
    """Write one Kokoro wav. Warm worker first, then CLI, then one-shot.

    ``misaki_ready`` is the once-per-reply install result. Sentence clips omit
    it and read the cache ``_prepare_kokoro_misaki`` filled for this generation.
    """
    if _playback_blocked(generation):
        return None
    if misaki_ready is None:
        misaki_ready = _cached_misaki_ready(generation, kokoro_g2p_lang(text, voice))
        if misaki_ready is None:
            misaki_ready = True
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    model_path, voices_path = _resolve_kokoro_model_files(on_status=on_status)
    if os.path.isfile(model_path) and os.path.isfile(voices_path):
        wav_path, allow_fallback = _kokoro_warm_to_file(
            text, voice, speed, model_path, voices_path, generation,
            on_status=on_status, misaki_ready=misaki_ready,
        )
        if wav_path or not allow_fallback:
            return wav_path
    if py_exe:
        bin_dir = os.path.dirname(py_exe)
        cand_cli = os.path.join(bin_dir, "kokoro.exe" if sys.platform == "win32" else "kokoro")
        if os.path.isfile(cand_cli) and os.access(cand_cli, os.X_OK):
            wav_path = _kokoro_cli_to_file(cand_cli, text, voice, speed, generation)
            if wav_path or _playback_blocked(generation):
                return wav_path
        if os.path.isfile(model_path) and os.path.isfile(voices_path):
            return _kokoro_oneshot_to_file(
                py_exe, text, voice, speed, model_path, voices_path, generation,
                on_status=on_status, misaki_ready=misaki_ready,
            )
    return None


def _speak_kokoro_local(
    text: str,
    voice: str = _KOKORO_FALLBACK_VOICE,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
) -> None:
    """Synthesize text using local Kokoro and play it.

    The warm worker keeps ONNX and the voices file loaded. The CLI and
    ``python -c`` paths run only when that worker cannot.
    """
    log.info("Speaking via local Kokoro (voice=%s, speed=%.2f)", voice, speed)
    if _playback_blocked(generation):
        return
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        log.warning(
            "Local Kokoro TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed, generation=generation)
        return
    # Latin text uses English espeak; other text uses this voice's Misaki lang.
    # ``voice`` is still the speaker id passed to Kokoro.create. Install once
    # per language per reply. Stop during install must not fall through to OS
    # speech. Install failure does not refuse the speak.
    lang = kokoro_g2p_lang(text, voice)
    log.info("Kokoro G2P lang=%s for voice=%s", lang, voice)
    # Non-English voices were phonemized with espeak-ng inside kokoro-onnx, so
    # ja read kanji as "chinese letter" and fr/es missed Kokoro's phone map.
    # Install once, then the warm worker (or the one-shot script) passes
    # Misaki phonemes. Install failure does not refuse the speak. Stop during
    # install returns None and must not fall through to OS speech.
    misaki_ready = _prepare_kokoro_misaki(text, voice, on_status, generation, py_exe=py_exe)
    if misaki_ready is None:
        return
    wav_path = _kokoro_audio_file(
        text, voice, speed, on_status, generation, misaki_ready=misaki_ready,
    )
    if wav_path:
        try:
            _play_audio_file(wav_path, generation=generation)
        finally:
            _release_temp(wav_path)
        return
    if _playback_blocked(generation):
        return
    log.warning(
        "Local Kokoro engine not available in configured venv. "
        "Install with: %s. Falling back to OS speech.",
        KOKORO_PIP_INSTALL,
    )
    _speak_system(text, speed=speed, generation=generation)


def _piper_audio_file(
    text: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    generation: int | None,
) -> str | None:
    """One Piper wav via the CLI. Piper is not kept warm; prefetch still overlaps it with playback."""
    if _playback_blocked(generation):
        return None
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        return None
    model_file = _resolve_piper_model_file(voice, on_status=on_status)
    bin_dir = os.path.dirname(py_exe)
    cand_bin = os.path.join(bin_dir, "piper.exe" if sys.platform == "win32" else "piper")
    piper_bin = cand_bin if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK) else None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    length_scale = round(1.0 / max(0.2, min(5.0, speed)), 2)
    if piper_bin:
        cmd = [piper_bin, "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]
    else:
        cmd = [py_exe, "-m", "piper", "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _popen_for_speech(
            cmd,
            generation,
            slot="synth",
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc is None:
            _release_temp(tmp_wav)
            return None
        try:
            _unused_stdout, stderr = proc.communicate(input=text, timeout=30)
            if proc.returncode != 0:
                log.warning("Piper process failed (code %d): %s", proc.returncode, stderr)
        except Exception:
            proc.kill()
            raise
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            return tmp_wav
        log.warning("Piper produced empty audio for voice %s", voice)
    except FileNotFoundError:
        log.warning(
            "Local Piper executable not found in configured venv. Install via 'uv pip install piper-tts'."
        )
    except Exception as exc:
        log.warning("Piper synthesis error: %s", exc)
    finally:
        _clear_speech_proc(proc, "synth")
    _release_temp(tmp_wav)
    return None


def _speak_piper_local(
    text: str,
    voice: str = _PIPER_FALLBACK_VOICE,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
    generation: int | None = None,
) -> None:
    """Synthesize text using local Piper and play it."""
    log.info("Speaking via local Piper (voice=%s, speed=%.2f)", voice, speed)
    if _playback_blocked(generation):
        return
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not (resolve_venv_python(venv_dir) if venv_dir else None):
        log.warning(
            "Local Piper TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed, generation=generation)
        return
    wav_path = _piper_audio_file(text, voice, speed, on_status, generation)
    if wav_path:
        try:
            _play_audio_file(wav_path, generation=generation)
        finally:
            _release_temp(wav_path)
        return
    if _playback_blocked(generation):
        return
    log.warning("Piper synthesis failed for voice %s; falling back to OS speech", voice)
    _speak_system(text, speed=speed, generation=generation)


def _synthesize_sentence_clip(
    sentence: str,
    provider: str,
    voice: str,
    speed: float,
    model: str,
    endpoint_url: str,
    api_key: str,
    on_status: Callable[[str], None] | None,
    generation: int,
) -> _ReadyClip | None:
    """Synthesize one sentence without playing it.

    ``None`` means the utterance was cancelled. A clip with ``speak_system``
    is the OS-speech fallback and is played by the consumer so order is kept.
    """
    if _playback_blocked(generation):
        return None
    if provider == "system":
        return _ReadyClip(None, sentence, 0, True)

    path: str | None = None
    if provider == "kokoro":
        path = _kokoro_audio_file(sentence, voice, speed, on_status, generation)
    elif provider == "piper":
        path = _piper_audio_file(sentence, voice, speed, on_status, generation)
    elif provider == "endpoint" and endpoint_url:
        path = _download_endpoint_speech(
            sentence, endpoint_url, api_key, model, voice, speed=speed, generation=generation,
            on_status=on_status,
        )
    if _playback_blocked(generation):
        _release_temp(path)
        return None
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        return _ReadyClip(path, sentence, os.path.getsize(path), False)
    _release_temp(path)
    if provider == "endpoint" and not endpoint_url:
        log.warning("No endpoint URL available for TTS; falling back to OS system speech.")
    return _ReadyClip(None, sentence, 0, True)


def _run_sentence_pipeline(
    sentences: list[str],
    provider: str,
    voice: str,
    speed: float,
    model: str,
    endpoint_url: str,
    api_key: str,
    on_status: Callable[[str], None] | None,
    generation: int,
    *,
    max_clips: int = SPEECH_READY_MAX_CLIPS,
    max_bytes: int = SPEECH_READY_MAX_BYTES,
) -> None:
    """Play sentences in order while synthesis keeps running ahead.

    The producer does not wait for playback. The consumer never waits for the
    rest of the reply. Backpressure only applies once several clips are already
    waiting, so a one-word sentence cannot stall the long sentence after it.

    Kokoro's Misaki install runs once per G2P language inside the producer.
    ``kokoro_g2p_lang`` picks that language per sentence, so Latin text on a
    Japanese voice does not install Misaki. Stop during an install returns
    without an OS-speech clip. Later sentences of the same language reuse the
    cached probe.
    """
    global _ready_queue
    ready = _ReadyQueue(max_clips, max_bytes)
    with _speech_lock:
        if _playback_blocked_locked(generation):
            return
        _ready_queue = ready

    def _produce() -> None:
        try:
            for index, sentence in enumerate(sentences):
                if _playback_blocked(generation):
                    return
                if provider == "kokoro" and _prepare_kokoro_misaki(
                    sentence, voice, on_status, generation,
                ) is None:
                    return
                clip = _synthesize_sentence_clip(
                    sentence,
                    provider,
                    voice,
                    speed,
                    model,
                    endpoint_url,
                    api_key,
                    on_status if index == 0 else None,
                    generation,
                )
                if clip is None:
                    return
                if not ready.put(clip, generation):
                    _release_temp(clip.path)
                    return
        finally:
            ready.close()

    # dedicated: this loop blocks on synthesis and on the ready-queue cap, and
    # the consumer thread joins the utterance, not this task.
    run_in_background(_produce, dedicated=True, name="tts-prefetch")
    while True:
        clip = ready.get(generation)
        if clip is None:
            break
        try:
            if clip.path:
                _play_audio_file(clip.path, generation=generation)
            elif clip.speak_system:
                _speak_system(clip.text, speed=speed, generation=generation)
        finally:
            _release_temp(clip.path)


def speak_text_async(
    text: str,
    on_complete: Callable[[], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    ctx: Any = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    enabled: bool | None = None,
) -> None:
    """Synthesize and speak text in a background thread.

    ``on_status`` receives short user-visible lines (model download, fallback).
    The chat panel posts those onto the sidebar status field.

    Keyword overrides are for Settings → Speech → Test voice, which speaks the
    controls on screen before OK writes them. Chat leaves them unset and reads
    ``audio.tts_*``. Local Kokoro picks Misaki vs English espeak inside
    :func:`_speak_kokoro_local` via :func:`kokoro_g2p_lang`.

    When sentence mode is on and *ctx* is the sidebar's component context,
    sentences are split here — on the caller thread — before the worker
    starts. BreakIterator is a UNO service and must not be used from the
    audio thread. Without *ctx* the whole reply is one clip (unit tests and
    any caller that is not on the UNO thread). Test voice does not pass *ctx*.
    """
    tts_on = bool(get_config("audio.tts_enabled")) if enabled is None else bool(enabled)
    if not tts_on:
        log.debug("speak_text_async: TTS is disabled (audio.tts_enabled=False)")
        return

    clean = clean_text_for_speech(text)
    if not clean:
        log.debug("speak_text_async: No speakable text after cleaning")
        return

    sentences: list[str] | None = None
    if sentence_speak_enabled() and ctx is not None:
        sentences = sentences_for_speech(clean, ctx)
        if not sentences:
            log.debug("speak_text_async: sentence split produced nothing to say")
            return

    generation = _begin_utterance()
    log.info(
        "speak_text_async: queued speech for %d chars (%s)",
        len(clean),
        f"{len(sentences)} sentences" if sentences is not None else "one clip",
    )
    chosen_provider = provider
    chosen_model = model
    chosen_voice = voice
    chosen_speed = speed

    def _worker() -> None:
        try:
            if _playback_blocked(generation):
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
                "TTS worker executing: provider=%s, speed=%.2f, voice=%s, model=%s, generation=%s",
                provider_code,
                speed_val,
                voice_name,
                model_name,
                generation,
            )

            if sentences is not None:
                endpoint_url = ""
                api_key = ""
                if provider_code == "endpoint":
                    from plugin.framework.config import get_current_endpoint
                    endpoint_url = get_current_endpoint() or ""
                    api_key = get_api_key_for_endpoint(endpoint_url) if endpoint_url else ""
                _run_sentence_pipeline(
                    sentences,
                    provider_code,
                    voice_name,
                    speed_val,
                    model_name,
                    endpoint_url,
                    api_key,
                    on_status,
                    generation,
                )
                return

            if provider_code == "system":
                _speak_system(clean, speed=speed_val, generation=generation)
            elif provider_code == "kokoro":
                _speak_kokoro_local(
                    clean, voice=voice_name, speed=speed_val, on_status=on_status, generation=generation,
                )
            elif provider_code == "piper":
                _speak_piper_local(
                    clean, voice=voice_name, speed=speed_val, on_status=on_status, generation=generation,
                )
            elif provider_code == "endpoint":
                from plugin.framework.config import get_current_endpoint
                url = get_current_endpoint()
                api_key = get_api_key_for_endpoint(url)
                log.info("TTS endpoint resolved: url=%s, model=%s, has_key=%s", url, model_name, bool(api_key))
                if url:
                    _speak_endpoint(
                        clean, url, api_key, model=model_name, voice=voice_name,
                        speed=speed_val, generation=generation, on_status=on_status,
                    )
                else:
                    log.warning("No endpoint URL available for TTS; falling back to OS system speech.")
                    _speak_system(clean, speed=speed_val, generation=generation)
            else:
                _speak_system(clean, speed=speed_val, generation=generation)
        except Exception as exc:
            log.exception("speak_text_async worker error: %s", exc)
        finally:
            if _end_utterance(generation) and on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

    run_in_background(_worker, dedicated=True, name="tts-speak")
