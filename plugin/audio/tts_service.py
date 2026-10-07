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
from collections import deque
from typing import Any, Callable, ClassVar

from plugin.audio.kokoro_g2p import (
    KOKORO_ONNX_SCRIPT,
    KOKORO_PIP_INSTALL,
    ensure_kokoro_misaki,
    kokoro_lang_uses_misaki,
)
from plugin.audio.kokoro_pool import (
    get_kokoro_pool,
    resolve_kokoro_python,
)
from plugin.audio.tts_endpoint import _speak_endpoint
from plugin.audio.tts_models import (
    _clear_tts_status,
    _notify_tts_status,
    _resolve_kokoro_model_files,
    _resolve_piper_model_file,
    clear_generation_downloads,
)
from plugin.audio.voice_catalog import (
    KOKORO_FALLBACK_VOICE as _KOKORO_FALLBACK_VOICE,
    PIPER_FALLBACK_VOICE as _PIPER_FALLBACK_VOICE,
)
from plugin.audio.tts_voices import (
    clean_provider_name,
    clean_voice_name,
    get_scoped_tts_voice,
    kokoro_g2p_lang,
    parse_tts_speed,
)
from plugin.framework import config


def get_config(*args: Any, **kwargs: Any) -> Any:
    return config.get_config(*args, **kwargs)


def get_config_str(*args: Any, **kwargs: Any) -> Any:
    return config.get_config_str(*args, **kwargs)
from plugin.framework.i18n import _
from plugin.framework.worker_pool import run_in_background

__all__ = [
    "clean_text_for_speech",
    "is_speaking",
    "sentences_for_speech",
    "speak_text_async",
    "stop_speech",
    "_release_temp",
    "_terminate_proc",
]

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
# long sentence (and a few after it) are already synthesized.
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
    """Bounded, thread-safe FIFO buffer of synthesized sentence clips."""

    max_clips: int
    max_bytes: int
    _lock: threading.Lock
    _not_empty: threading.Condition
    _not_full: threading.Condition
    _clips: deque[_ReadyClip]
    _closed: bool
    _bytes: int

    def __init__(self, max_clips: int, max_bytes: int) -> None:
        self.max_clips = max(2, max_clips)
        self.max_bytes = max(1024 * 1024, max_bytes)
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._not_full = threading.Condition(self._lock)
        self._clips = deque()
        self._closed = False
        self._bytes = 0

    def put(self, clip: _ReadyClip, generation: int) -> bool:
        """Enqueue one clip, blocking if full until a clip plays or generation changes."""
        with self._lock:
            while True:
                if _playback_blocked(generation) or self._closed:
                    return False
                if len(self._clips) < 2:
                    break
                if len(self._clips) < self.max_clips and (self._bytes + clip.nbytes) <= self.max_bytes:
                    break
                self._not_full.wait(timeout=0.1)
            self._clips.append(clip)
            self._bytes += clip.nbytes
            self._not_empty.notify()
            return True

    def get(self, generation: int) -> _ReadyClip | None:
        """Pop the next clip in playback order, blocking until synthesized or closed."""
        with self._lock:
            while True:
                if _playback_blocked(generation):
                    return None
                if self._clips:
                    clip = self._clips.popleft()
                    self._bytes = max(0, self._bytes - clip.nbytes)
                    self._not_full.notify()
                    return clip
                if self._closed:
                    return None
                self._not_empty.wait(timeout=0.1)

    def close(self) -> None:
        """Mark that no more clips will be produced for this utterance."""
        with self._lock:
            self._closed = True
            self._not_empty.notify_all()
            self._not_full.notify_all()

    def drain(self) -> list[str]:
        """Drop unplayed clips and return their file paths so Stop can unlink them."""
        with self._lock:
            self._closed = True
            paths: list[str] = [clip.path for clip in self._clips if clip.path]
            self._clips.clear()
            self._bytes = 0
            self._not_empty.notify_all()
            self._not_full.notify_all()
            return paths


def _playback_blocked_locked(generation: int | None) -> bool:
    if generation is None:
        return bool(_speech_active and _speech_cancelled.is_set())
    if generation != _speech_generation:
        return True
    return False


def _playback_blocked(generation: int | None) -> bool:
    """True if speech has been cancelled or the utterance generation has changed."""
    with _speech_lock:
        return _playback_blocked_locked(generation)


def _proc_running(proc: subprocess.Popen[Any] | None) -> bool:
    return proc is not None and proc.poll() is None


def _is_spd_say(proc: subprocess.Popen[Any] | None) -> bool:
    args = getattr(proc, "args", None)
    return isinstance(args, (list, tuple)) and bool(args) and os.path.basename(str(args[0])) == "spd-say"


def _signal_stop_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Signal a speech process to stop immediately without blocking the caller."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        log.debug("stdin close failed", exc_info=True)
    try:
        log.info("Terminating speech process (PID %s)", proc.pid)
        proc.terminate()
        if sys.platform != "win32":
            proc.kill()
    except Exception as exc:
        log.debug("speech terminate error: %s", exc)


def _reap_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Reap a terminated process in a background thread."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                proc.wait(timeout=5)
        else:
            proc.wait(timeout=5)
    except Exception as exc:
        log.debug("speech reap error: %s", exc)


def _terminate_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Terminate and reap a process (used in tests or synchronous cleanup paths)."""
    _signal_stop_proc(proc)
    _reap_proc(proc)


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

    Only strips real HTML/XML tags so math like 'if x<5 and y>3' is preserved.
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

    # Remove bold/italic * markers. Underscores are handled below.
    cleaned = re.sub(r"[*]{1,3}([^*]+)[*]{1,3}", r"\1", cleaned)

    # Remove real HTML/XML tags only
    cleaned = re.sub(r"</?[A-Za-z][\w:-]*(\s[^<>]*)?/?>", "", cleaned)

    # Remove raw URLs
    cleaned = re.sub(r"https?://\S+", "link", cleaned)

    # Underscores become spaces after URLs are gone
    cleaned = cleaned.replace("_", " ")

    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def stop_speech() -> None:
    """Stop playback, cancel in-flight synthesis, and delete clips not yet played.

    UI-thread safe: sends signals/kills immediately and delegates waiting/reaping,
    spd-say -S, and temp unlinking to a background worker.
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
        clear_generation_downloads(_speech_generation - 1)

    cancel_token: object | None = None
    try:
        from plugin.audio.kokoro_pool import get_kokoro_inflight_token

        cancel_token = get_kokoro_inflight_token()
    except Exception:
        log.debug("Kokoro token lookup failed", exc_info=True)

    # Non-blocking signal/kill on current thread
    stop_spd = _proc_running(play) and _is_spd_say(play)
    all_procs = ([play] if play is not None else []) + synths
    for proc in all_procs:
        _signal_stop_proc(proc)

    if cancel_token is not None:
        try:
            from plugin.audio.kokoro_pool import cancel_kokoro_inflight

            cancel_kokoro_inflight(cancel_token)
        except Exception:
            log.debug("Kokoro cancel failed", exc_info=True)

    def _bg_cleanup() -> None:
        for proc in all_procs:
            _reap_proc(proc)
        if stop_spd:
            try:
                subprocess.run(
                    ["spd-say", "-S"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception as exc:
                log.debug("spd-say stop failed: %s", exc)
        paths: list[str] = []
        if queue_ref is not None:
            paths.extend(queue_ref.drain())
        paths.extend(temps)
        for path in paths:
            _unlink_quiet(path)

    run_in_background(_bg_cleanup)


def _begin_utterance() -> int:
    """Cancel whatever is speaking and return the generation id for the new one."""
    global _speech_active
    stop_speech()
    with _speech_lock:
        _speech_active = True
        _speech_cancelled.clear()
        return _speech_generation


def _end_utterance(generation: int) -> bool:
    """Tear down per-reply state when the worker thread finishes."""
    global _speech_active
    with _speech_lock:
        if generation != _speech_generation:
            return False
        _speech_active = False
        _reply_misaki_ready.pop(generation, None)
        clear_generation_downloads(generation)
        return True


def _new_speech_temp(suffix: str, generation: int | None) -> str | None:
    """Create a tracked temp file for audio."""
    with _speech_lock:
        if _playback_blocked_locked(generation):
            return None
        fd, path = tempfile.mkstemp(prefix="writeragent_speech_", suffix=suffix)
        os.close(fd)
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
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[Any] | None:
    """Spawn a play or synth process and register it so stop_speech can kill it.

    Does not hold _speech_lock across Popen so is_speaking/_playback_blocked
    are not blocked during process startup.
    """
    global _play_proc
    with _speech_lock:
        if _playback_blocked_locked(generation):
            log.info("Speech cancelled before process spawn")
            return None

    proc = subprocess.Popen(
        cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        env=env,
    )

    with _speech_lock:
        if _playback_blocked_locked(generation):
            log.info("Speech cancelled during process spawn, killing process")
            _signal_stop_proc(proc)
            run_in_background(lambda: _reap_proc(proc))
            return None
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
    """True when audio.tts_sentence_mode is active."""
    val = get_config("audio.tts_sentence_mode")
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("false", "0", "no", "off"):
            return False
        return True
    if val is None:
        return True
    from plugin.framework.config_schema import as_bool

    return as_bool(val)


def uses_sentence_by_sentence(provider: str) -> bool:
    """True only for local Kokoro and Piper."""
    return clean_provider_name(provider) in ("kokoro", "piper")


def _speech_locale_key() -> str:
    """BCP-47 tag for the grammar sentence splitter (en_US -> en-US)."""
    try:
        from plugin.framework.i18n import get_active_locale

        loc = get_active_locale() or "en_US"
    except Exception:
        loc = "en_US"
    return loc.replace("_", "-")


def sentences_for_speech(text: str, ctx: Any) -> list[str]:
    """Split text into sentences using the BreakIterator sentence splitter."""
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


def _get_player_candidates(file_path: str) -> list[tuple[str, list[str], dict[str, str] | None]]:
    """Determine candidate audio player commands ordered by file format compatibility."""
    if sys.platform == "darwin":
        return [("afplay", ["afplay", file_path], None)]
    if sys.platform == "win32":
        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "(New-Object Media.SoundPlayer $env:WA_AUDIO_PATH).PlaySync()",
        ]
        env = {**os.environ, "WA_AUDIO_PATH": file_path}
        return [("powershell", ps_cmd, env)]

    # Linux / Unix
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix == ".wav":
        order = ["ffplay", "mpv", "pw-play", "paplay", "aplay"]
    elif suffix == ".mp3":
        order = ["ffplay", "mpv", "mpg123"]
    else:
        order = ["ffplay", "mpv", "pw-play", "paplay", "aplay", "mpg123"]

    candidates: list[tuple[str, list[str], dict[str, str] | None]] = []
    for player in order:
        if not shutil.which(player):
            continue
        if player == "ffplay":
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]
        elif player == "mpv":
            cmd = ["mpv", "--no-video", file_path]
        elif player == "mpg123":
            cmd = ["mpg123", "-q", file_path]
        elif player in ("pw-play", "paplay", "aplay"):
            cmd = [player, file_path]
        else:
            cmd = [player, file_path]
        candidates.append((player, cmd, None))
    return candidates


def _play_audio_file(file_path: str, generation: int | None = None) -> None:
    """Play an audio file using available OS command-line utilities.

    Tries candidates appropriate for the file extension in order, checking
    exit codes and falling back if a player fails.
    """
    candidates = _get_player_candidates(file_path)
    if not candidates:
        log.warning("No audio player found on system to play: %s", file_path)
        return

    for player_name, cmd, env in candidates:
        if _playback_blocked(generation):
            return
        proc: subprocess.Popen[Any] | None = None
        try:
            log.info("Playing audio with command: %s", " ".join(cmd))
            proc = _popen_for_speech(cmd, generation, slot="play", env=env)
            if proc is None:
                return
            ret = proc.wait()
            if ret == 0:
                log.info("Audio playback completed successfully")
                return
            if _playback_blocked(generation):
                return
            log.warning("Audio player '%s' exited with code %d; trying next candidate", player_name, ret)
        except Exception as exc:
            log.warning("_play_audio_file playback error with '%s': %s", player_name, exc)
        finally:
            _clear_speech_proc(proc, "play")

    log.warning("All audio players failed for: %s", file_path)


def _chunk_text(text: str, max_chunk: int = 4000) -> list[str]:
    """Split very long text into chunks along space boundaries."""
    if len(text) <= max_chunk:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chunk:
        split_idx = remaining.rfind(" ", 0, max_chunk)
        if split_idx == -1:
            split_idx = max_chunk
        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def _speak_system(text: str, speed: float = 1.0, generation: int | None = None) -> None:
    """Speak text using built-in OS speech synthesis utilities."""
    if _playback_blocked(generation):
        return

    temp_txt: str | None = None
    try:
        env = None
        stdin_input: str | None = None
        cmd: list[str] | None = None

        if sys.platform == "darwin":
            rate = int(175 * speed)
            temp_txt = _new_speech_temp(".txt", generation)
            if temp_txt is None:
                return
            with open(temp_txt, "w", encoding="utf-8") as f:
                f.write(text)
            cmd = ["/usr/bin/say", "-r", str(rate), "-f", temp_txt]
        elif sys.platform == "win32":
            rate_int = int((speed - 1.0) * 5)
            rate_int = max(-10, min(10, rate_int))
            temp_txt = _new_speech_temp(".txt", generation)
            if temp_txt is None:
                return
            with open(temp_txt, "w", encoding="utf-8") as f:
                f.write(text)
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$synth.Rate = {rate_int}; "
                "$txt = [System.IO.File]::ReadAllText($env:WA_SAPI_FILE, [System.Text.Encoding]::UTF8); "
                "$synth.Speak($txt)"
            )
            cmd = ["powershell", "-NoProfile", "-Command", ps_script]
            env = {**os.environ, "WA_SAPI_FILE": temp_txt}
        else:
            # Linux native
            if shutil.which("espeak"):
                speed_wpm = int(160 * speed)
                cmd = ["espeak", "-s", str(speed_wpm), "--stdin"]
                stdin_input = text
            elif shutil.which("spd-say"):
                rate_pct = int((speed - 1.0) * 100)
                rate_pct = max(-100, min(100, rate_pct))
                # spd-say takes text as argv; chunk if text is long
                chunks = _chunk_text(text, 4000)
                for chunk in chunks:
                    if _playback_blocked(generation):
                        return
                    c_cmd = ["spd-say", "-r", str(rate_pct), "-w", "--", chunk]
                    chunk_proc: subprocess.Popen[Any] | None = None
                    try:
                        chunk_proc = _popen_for_speech(c_cmd, generation, slot="play")
                        if chunk_proc is None:
                            return
                        chunk_proc.wait()
                    except OSError as exc:
                        log.warning("_speak_system spd-say OSError: %s", exc)
                    finally:
                        _clear_speech_proc(chunk_proc, "play")
                return

        if not cmd:
            log.warning("No OS native text-to-speech utility (say/spd-say/espeak) found on system.")
            return

        proc: subprocess.Popen[Any] | None = None
        try:
            log.info("Speaking via system command: %s", " ".join(cmd[:3]))
            proc = _popen_for_speech(
                cmd,
                generation,
                slot="play",
                stdin=subprocess.PIPE if stdin_input is not None else None,
                env=env,
                text=True if stdin_input is not None else False,
                encoding="utf-8" if stdin_input is not None else None,
                errors="replace" if stdin_input is not None else None,
            )
            if proc is None:
                return
            if stdin_input is not None:
                proc.communicate(input=stdin_input)
            else:
                proc.wait()
            log.info("System speech playback completed")
        except OSError as exc:
            log.warning("_speak_system OS error: %s", exc)
        except Exception as exc:
            log.debug("_speak_system error: %s", exc, exc_info=True)
        finally:
            _clear_speech_proc(proc, "play")
    finally:
        if temp_txt:
            _release_temp(temp_txt)


def _venv_python() -> str | None:
    """Resolve configured venv Python executable."""
    return resolve_kokoro_python()


def _notify_kokoro_g2p_fallback(
    stderr_text: str,
    misaki_ready: bool | None,
    on_status: Callable[[str], None] | None,
) -> None:
    """Surface Misaki fallback to the chat panel."""
    if misaki_ready is False and on_status is not None:
        _notify_tts_status(_("Misaki not found; Kokoro used English phonemes"), on_status)
    elif "Misaki G2P failed" in stderr_text and on_status is not None:
        _notify_tts_status(_("Kokoro used English phonemes for non-English voice"), on_status)


def _remember_misaki_ready(generation: int | None, lang: str, ready: bool) -> None:
    if generation is None:
        return
    _reply_misaki_ready.setdefault(generation, {})[lang] = ready


def _cached_misaki_ready(generation: int | None, lang: str) -> bool | None:
    if generation is None:
        return None
    table = _reply_misaki_ready.get(generation)
    if table is None:
        return None
    return table.get(lang)


def _prepare_kokoro_misaki(
    text: str,
    voice: str,
    on_status: Callable[[str], None] | None,
    generation: int | None,
    py_exe: str | None = None,
) -> bool | None:
    """Install Misaki at most once per G2P language for this reply."""
    lang = kokoro_g2p_lang(text, voice)
    cached = _cached_misaki_ready(generation, lang)
    if cached is not None:
        return cached
    if not kokoro_lang_uses_misaki(lang):
        _remember_misaki_ready(generation, lang, True)
        return True
    if py_exe is None:
        py_exe = _venv_python()
    if not py_exe:
        _remember_misaki_ready(generation, lang, False)
        return False

    def _cancelled() -> bool:
        return _speech_cancelled.is_set() or _playback_blocked(generation)

    def _run_misaki_cmd(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        proc = _popen_for_speech(
            cmd,
            generation,
            slot="synth",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc is None:
            return None
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except Exception as exc:
            proc.kill()
            try:
                proc.wait(timeout=5.0)
            except Exception:
                pass
            log.warning("Kokoro phonemizer command failed: %s", exc)
            return None
        finally:
            _clear_speech_proc(proc, "synth")

    ready = ensure_kokoro_misaki(
        py_exe,
        lang,
        on_status=lambda message: _notify_tts_status(message, on_status, progress=True),
        cancelled=_cancelled,
        run_cmd=_run_misaki_cmd,
    )
    if ready is None or _cancelled():
        return None
    _clear_tts_status(on_status)
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
    """Synthesize with the keep-alive worker."""
    if _playback_blocked(generation):
        return None, False
    try:
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
        },
        cancel_check=lambda: _playback_blocked(generation),
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
        warning = result.get("warning") or ""
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
    """Cold python -c Kokoro load. Used when the warm worker cannot start."""
    if _playback_blocked(generation):
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    lang = kokoro_g2p_lang(text, voice)
    # Pass '-' for text argument and feed text via stdin to avoid argv limits
    cmd = [py_exe, "-c", KOKORO_ONNX_SCRIPT, "-", voice, str(speed), tmp_wav, model_path, voices_path, lang]
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _popen_for_speech(
            cmd,
            generation,
            slot="synth",
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc is None:
            _release_temp(tmp_wav)
            return None
        _unused_stdout, stderr = proc.communicate(input=text)
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
    cand_cli: str,
    text: str,
    voice: str,
    speed: float,
    generation: int | None,
) -> str | None:
    """Kokoro CLI fallback."""
    if _playback_blocked(generation):
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    cmd = [cand_cli, "--voice", voice, "--speed", str(speed), "--output", tmp_wav, text]
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
    """Write one Kokoro wav. Warm worker first, then CLI, then one-shot."""
    if _playback_blocked(generation):
        return None
    if misaki_ready is None:
        misaki_ready = _cached_misaki_ready(generation, kokoro_g2p_lang(text, voice))
        if misaki_ready is None:
            misaki_ready = True
    py_exe = _venv_python()
    model_path, voices_path = _resolve_kokoro_model_files(
        on_status=on_status,
        generation=generation,
        cancelled=lambda: _playback_blocked(generation),
    )
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
    """Synthesize text using local Kokoro and play it."""
    log.info("Speaking via local Kokoro (voice=%s, speed=%.2f)", voice, speed)
    if _playback_blocked(generation):
        return
    py_exe = _venv_python()
    if not py_exe:
        log.warning(
            "Local Kokoro TTS requires a configured Python venv. "
            "Please configure your venv path in Settings -> Python."
        )
        _speak_system(text, speed=speed, generation=generation)
        return

    lang = kokoro_g2p_lang(text, voice)
    log.info("Kokoro G2P lang=%s for voice=%s", lang, voice)
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
    """One Piper wav via CLI. Timeout scales with text length."""
    if _playback_blocked(generation):
        return None
    py_exe = _venv_python()
    if not py_exe:
        return None
    model_file = _resolve_piper_model_file(
        voice,
        on_status=on_status,
        generation=generation,
        cancelled=lambda: _playback_blocked(generation),
    )
    if not model_file or not os.path.isfile(model_file):
        return None

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

    # Generous scaled timeout based on character count
    timeout = max(30.0, min(600.0, 30.0 + len(text) * 0.2))

    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _popen_for_speech(
            cmd,
            generation,
            slot="synth",
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc is None:
            _release_temp(tmp_wav)
            return None
        try:
            _unused_stdout, stderr = proc.communicate(input=text, timeout=timeout)
            if proc.returncode != 0:
                log.warning("Piper process failed (code %d): %s", proc.returncode, stderr)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5.0)
            except Exception:
                pass
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
    if not _venv_python():
        log.warning(
            "Local Piper TTS requires a configured Python venv. "
            "Please configure your venv path in Settings -> Python."
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
    on_status: Callable[[str], None] | None,
    generation: int,
) -> _ReadyClip | None:
    """Synthesize one sentence without playing it for sentence prefetch."""
    if _playback_blocked(generation):
        return None

    path: str | None = None
    if provider == "kokoro":
        path = _kokoro_audio_file(sentence, voice, speed, on_status, generation)
    elif provider == "piper":
        path = _piper_audio_file(sentence, voice, speed, on_status, generation)

    if _playback_blocked(generation):
        _release_temp(path)
        return None
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        return _ReadyClip(path, sentence, os.path.getsize(path), False)
    _release_temp(path)
    return _ReadyClip(None, sentence, 0, True)


def _run_sentence_pipeline(
    sentences: list[str],
    provider: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    generation: int,
    *,
    max_clips: int = SPEECH_READY_MAX_CLIPS,
    max_bytes: int = SPEECH_READY_MAX_BYTES,
) -> None:
    """Play sentences in order while synthesis keeps running ahead."""
    global _ready_queue
    ready = _ReadyQueue(max_clips, max_bytes)
    with _speech_lock:
        if _playback_blocked_locked(generation):
            return
        _ready_queue = ready

    def _produce() -> None:
        done = 0
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
                    on_status if index == 0 else None,
                    generation,
                )
                if clip is None:
                    return
                if not ready.put(clip, generation):
                    _release_temp(clip.path)
                    return
                done = index + 1
        except Exception:
            log.exception("Prefetch thread failed")
            rest = sentences[done:]
            if rest and not _playback_blocked(generation):
                ready.put(_ReadyClip(None, " ".join(rest), 0, True), generation)
        finally:
            ready.close()

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
    """Synthesize and speak text in a background thread."""
    tts_on = bool(get_config("audio.tts_enabled")) if enabled is None else bool(enabled)
    if not tts_on:
        log.debug("speak_text_async: TTS is disabled (audio.tts_enabled=False)")
        return

    clean = clean_text_for_speech(text)
    if not clean:
        log.debug("speak_text_async: No speakable text after cleaning")
        return

    chosen_provider = provider
    chosen_model = model
    chosen_voice = voice
    chosen_speed = speed
    active_provider = chosen_provider if chosen_provider else str(get_config("audio.tts_provider") or "system")
    sentences: list[str] | None = None
    if (
        sentence_speak_enabled()
        and ctx is not None
        and uses_sentence_by_sentence(active_provider)
    ):
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

            if sentences is not None and uses_sentence_by_sentence(provider_code):
                _run_sentence_pipeline(
                    sentences,
                    provider_code,
                    voice_name,
                    speed_val,
                    on_status,
                    generation,
                )
            elif provider_code == "kokoro":
                _speak_kokoro_local(
                    clean, voice=voice_name, speed=speed_val, on_status=on_status, generation=generation,
                )
            elif provider_code == "piper":
                _speak_piper_local(
                    clean, voice=voice_name, speed=speed_val, on_status=on_status, generation=generation,
                )
            elif provider_code == "endpoint":
                from plugin.framework.config import get_api_key_for_endpoint, get_current_endpoint

                endpoint_url = get_current_endpoint() or ""
                api_key = get_api_key_for_endpoint(endpoint_url) if endpoint_url else ""
                _speak_endpoint(
                    clean,
                    endpoint_url,
                    api_key,
                    model_name,
                    voice_name,
                    speed=speed_val,
                    generation=generation,
                    on_status=on_status,
                )
            else:
                _speak_system(clean, speed=speed_val, generation=generation)
        except Exception as exc:
            log.exception("speak_text_async worker error: %s", exc)
        finally:
            if _end_utterance(generation) and on_complete:
                try:
                    on_complete()
                except Exception:
                    log.debug("on_complete callback error", exc_info=True)

    run_in_background(_worker, dedicated=True, name="tts-speak")
