# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text-to-Speech (TTS) synthesis and playback service for WriterAgent."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from typing import Any, Callable, ClassVar, cast

from plugin.audio.kokoro_g2p import (
    G2P_FAILED_MARK,
    KOKORO_PIP_INSTALL,
    ensure_kokoro_misaki,
    kokoro_g2p_lang,
    kokoro_lang_uses_misaki,
)
from plugin.audio.kokoro_pool import (
    cancel_kokoro_inflight,
    get_kokoro_inflight_token,
    get_kokoro_pool,
    resolve_kokoro_python,
)
from plugin.audio.tts_endpoint import _download_endpoint_speech
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
    parse_tts_speed,
)
from plugin.framework import config
from plugin.framework.client.http_security import public_target
from plugin.framework.errors import best_effort
from plugin.framework.i18n import _, get_active_locale
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.worker_pool import run_in_background

# Wrappers so tests can patch get_config on this module without replacing
# plugin.framework.config for every other caller.
def get_config(*args: Any, **kwargs: Any) -> Any:
    return config.get_config(*args, **kwargs)


def get_config_str(*args: Any, **kwargs: Any) -> Any:
    return config.get_config_str(*args, **kwargs)

__all__ = [
    "clean_text_for_speech",
    "is_speaking",
    "sentences_for_speech",
    "speak_text_async",
    "stop_speech",
    "_release_temp",
]

log = logging.getLogger(__name__)

# Chat HTML that should not be read aloud. A bare "<b" plus prose ("a<b and c>d")
# is not a tag: attributes have to be name=value, so a comparison stays.
_SPEECH_HTML_TAGS = (
    "a|abbr|article|audio|b|blockquote|br|center|code|del|details|div|em|figcaption|"
    "figure|font|footer|h[1-6]|head|header|hr|html|i|img|ins|kbd|li|link|mark|meta|"
    "nav|ol|p|pre|s|samp|script|section|small|source|span|strong|style|sub|summary|"
    "sup|table|tbody|td|tfoot|th|thead|title|tr|tt|u|ul|var|video"
)
_SPEECH_HTML_TAG_RE = re.compile(
    "</?(?:" + _SPEECH_HTML_TAGS + r")\b"
    r"""(?:\s+[\w:-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))*\s*/?>""",
    re.IGNORECASE,
)
# Stars must hug the word. The old [*]{1,3}([^*]+)[*]{1,3} treated "2 * 3 * 4"
# as italic and deleted the multiplication signs.
_SPEECH_EMPH_RE = re.compile(r"(?<![\w*])\*{1,3}(\S(?:.*?\S)?)\*{1,3}(?![\w*])")
_CHUNK_BREAK_RE = re.compile(r"[.!?。！？](?:\s+|$)")
# Kokoro's CLI takes the utterance as the last argv. Past this, skip it and
# use the stdin one-shot path so a long reply cannot hit the OS argument limit.
_KOKORO_CLI_MAX_CHARS = 32000

# Playback and one-shot synthesis overlap during sentence prefetch, so they
# cannot share one Popen slot. Stop kills both. The objects in these slots are
# the Popen from ManagedSubprocess(raw_spawn=True), and Stop still signals that
# Popen directly.
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
    """Signal a speech process to stop immediately without blocking the caller.

    Unix sends SIGKILL only. terminate() and then an immediate kill() never
    lets a SIGTERM handler run, so the first signal did nothing. Windows
    terminate() is already TerminateProcess.
    """
    if proc is None:
        return
    if proc.stdin:
        best_effort(proc.stdin.close, "stdin close failed")
    try:
        log.info("Terminating speech process (PID %s)", proc.pid)
        if sys.platform == "win32":
            proc.terminate()
        else:
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
                ManagedSubprocess.run(
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


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        log.debug("Could not delete TTS temp %s", path, exc_info=True)


def is_speaking() -> bool:
    """Return True if TTS speech synthesis or playback is actively occurring."""
    with _speech_lock:
        if _speech_active or _proc_running(_play_proc):
            return True
        return any(_proc_running(proc) for proc in _synth_procs)


def clean_text_for_speech(text: str) -> str:
    """Prepare text for spoken synthesis by stripping markdown, code, and noise.

    Known HTML tags are removed. Comparisons such as ``x<5`` and ``a<b and c>d``
    stay, and ``2 * 3 * 4`` keeps its stars. List markers, quote markers, and
    table pipes are dropped so they are not read aloud.
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

    # Separator rows (|---|---|) and a lone --- rule. Then pipes become pauses.
    cleaned = re.sub(r"^[ \t|:-]*-[ \t|:-]*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("|", " ")

    # Blockquotes and unordered markers. A following space keeps "*italic*" intact.
    cleaned = re.sub(r"^[ \t]*(?:>[ \t]?)+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^[ \t]*[-*+][ \t]+", "", cleaned, flags=re.MULTILINE)

    cleaned = _SPEECH_EMPH_RE.sub(r"\1", cleaned)

    # Known tags only. The old any-letter pattern treated "a<b and c>d" as <b ...>.
    cleaned = _SPEECH_HTML_TAG_RE.sub("", cleaned)

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
        # The token is the job that was in flight during this Stop; reading it after the lock lets the next utterance publish a new token.
        cancel_token = get_kokoro_inflight_token()

    # Non-blocking signal/kill on current thread
    stop_spd = _proc_running(play) and _is_spd_say(play)
    all_procs = ([play] if play is not None else []) + synths
    for proc in all_procs:
        _signal_stop_proc(proc)

    if cancel_token is not None:
        best_effort(lambda: cancel_kokoro_inflight(cancel_token), "Kokoro cancel failed")

    def _bg_cleanup() -> None:
        for proc in all_procs:
            _reap_proc(proc)
        if stop_spd:
            try:
                ManagedSubprocess.run(
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

    # This job blocks in _reap_proc (wait timeout 5s per process, plus
    # spd-say -S timeout 2s) and the shared pool has two workers.
    # Bugfix: stop_speech raised if starting tts-cleanup thread failed, leaving procs and temps.
    # run_in_background was unwrapped; catch Exception and reap inline so cleanup always completes.
    try:
        run_in_background(_bg_cleanup, dedicated=True, name="tts-cleanup")
    except Exception:
        log.exception("stop_speech: failed to start cleanup thread; reaping inline")
        _bg_cleanup()


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
    """Spawn a play or synth process through ManagedSubprocess raw_spawn and return that Popen.

    Does not hold _speech_lock across Popen so is_speaking/_playback_blocked
    are not blocked during process startup.
    """
    global _play_proc
    with _speech_lock:
        if _playback_blocked_locked(generation):
            log.info("Speech cancelled before process spawn")
            return None

    managed = ManagedSubprocess(
        cmd,
        raw_spawn=True,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        env=env,
        label=f"tts-{slot}",
    )
    managed.start()
    proc = cast("subprocess.Popen[Any]", managed.popen)
    assert proc is not None

    with _speech_lock:
        if _playback_blocked_locked(generation):
            log.info("Speech cancelled during process spawn, killing process")
            _signal_stop_proc(proc)
            # That reap waits up to 5s and must not take a pool slot.
            run_in_background(lambda: _reap_proc(proc), dedicated=True, name="tts-reap")
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
            with contextlib.suppress(ValueError):
                _synth_procs.remove(proc)


def sentence_speak_enabled() -> bool:
    """True when audio.tts_sentence_mode is active."""
    val = get_config("audio.tts_sentence_mode")
    if val is None:
        return True
    from plugin.framework.config_schema import as_bool

    return as_bool(val)


def uses_sentence_by_sentence(provider: str) -> bool:
    """True only for local Kokoro and Piper."""
    return clean_provider_name(provider) in ("kokoro", "piper")


def _speech_locale_key() -> str:
    """BCP-47 tag for the grammar sentence splitter (en_US -> en-US)."""
    loc = get_active_locale() or "en_US"
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
        else:
            cmd = [player, file_path]
        candidates.append((player, cmd, None))
    return candidates


def _play_audio_file(file_path: str, generation: int | None = None) -> bool:
    """Play an audio file. True only when a player exits 0.

    Callers fall back to OS speech when this is False and the utterance
    was not cancelled. Returning None on every path used to look like
    success, so a missing player (Windows SoundPlayer cannot play mp3)
    never reached that fallback.
    """
    candidates = _get_player_candidates(file_path)
    if not candidates:
        log.warning("No audio player found on system to play: %s", file_path)
        return False

    for player_name, cmd, env in candidates:
        if _playback_blocked(generation):
            return False
        proc: subprocess.Popen[Any] | None = None
        try:
            log.info("Playing audio with command: %s", " ".join(cmd))
            proc = _popen_for_speech(cmd, generation, slot="play", env=env)
            if proc is None:
                return False
            ret = proc.wait()
            if ret == 0:
                log.info("Audio playback completed successfully")
                return True
            if _playback_blocked(generation):
                return False
            log.warning("Audio player '%s' exited with code %d; trying next candidate", player_name, ret)
        except Exception as exc:
            log.warning("_play_audio_file playback error with '%s': %s", player_name, exc)
        finally:
            _clear_speech_proc(proc, "play")

    log.warning("All audio players failed for: %s", file_path)
    return False


def _chunk_text(text: str, max_chunk: int = 4000) -> list[str]:
    """Split very long text so one speech request stays under host limits.

    A sentence end in the latter half of the window wins. Otherwise the
    last space, then a hard cut. spd-say and endpoint TTS both use this.
    """
    if max_chunk < 1:
        max_chunk = 1
    if len(text) <= max_chunk:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chunk:
        window = remaining[:max_chunk]
        split_idx = -1
        for match in _CHUNK_BREAK_RE.finditer(window):
            split_idx = match.end()
        # A break in the first half would make tiny clips and leave the long
        # tail for the next window. Prefer a space near the cap instead.
        if split_idx < max_chunk // 2:
            split_idx = window.rfind(" ")
            if split_idx <= 0:
                split_idx = max_chunk
        piece = remaining[:split_idx].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_idx:].strip()
        if not remaining:
            break
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk_cut(text: str, max_chunk: int) -> int:
    """Find the best cut index in text up to max_chunk characters."""
    if len(text) <= max_chunk:
        return len(text)
    window = text[:max_chunk]
    split_idx = -1
    for match in _CHUNK_BREAK_RE.finditer(window):
        split_idx = match.end()
    if split_idx < max_chunk // 2:
        split_idx = window.rfind(" ")
        if split_idx <= 0:
            split_idx = max_chunk
    return split_idx


def _endpoint_chunks(text: str) -> list[str]:
    """Split text for endpoint speech into pieces.

    Replies <= 4000 chars are split with _chunk_text(text, 4000). Longer replies
    use a first chunk capped near 600 characters followed by 4000-character chunks.
    """
    if len(text) <= 4000:
        return _chunk_text(text, 4000)
    cut = _chunk_cut(text, 600)
    first = text[:cut].strip()
    rest = text[cut:].strip()
    return [first] + _chunk_text(rest, 4000)


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
                        ret = chunk_proc.wait()
                    except OSError as exc:
                        log.warning("_speak_system spd-say OSError: %s", exc)
                        return
                    finally:
                        _clear_speech_proc(chunk_proc, "play")
                    # Bugfix: spd-say failure ignored its exit code and kept speaking remaining chunks.
                    # chunk_proc.wait() was uninspected; now check ret != 0, warn if unblocked, and abort.
                    if ret != 0:
                        if not _playback_blocked(generation):
                            log.warning("spd-say exited with code %s; not speaking the remaining text", ret)
                        return
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
    warning: str,
    misaki_ready: bool | None,
    on_status: Callable[[str], None] | None,
    *,
    g2p: str | None = None,
) -> None:
    """Surface Misaki fallback to the chat panel."""
    if on_status is None:
        return
    if g2p == "misaki":
        return
    if G2P_FAILED_MARK in warning:
        _notify_tts_status(_("Kokoro used English phonemes for non-English voice"), on_status)
    elif misaki_ready is False:
        _notify_tts_status(_("Misaki not found; Kokoro used English phonemes"), on_status)


def _remember_misaki_ready(generation: int | None, lang: str, ready: bool) -> None:
    if generation is None:
        return
    with _speech_lock:
        # stop_speech() clears this map under the same lock. A late write from
        # the prefetch thread used to setdefault a new entry for the generation
        # that had just been cancelled.
        if generation != _speech_generation:
            return
        _reply_misaki_ready.setdefault(generation, {})[lang] = ready


def _cached_misaki_ready(generation: int | None, lang: str) -> bool | None:
    if generation is None:
        return None
    with _speech_lock:
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
        try:
            result = _run_synth(cmd, generation, timeout=timeout, capture_stdout=True)
        except Exception as exc:
            log.warning("Kokoro phonemizer command failed: %s", exc)
            return None
        if result is None:
            return None
        code, stdout, stderr = result
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

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


def _kokoro_synth_timeout(text: str) -> float:
    """One-shot and CLI Kokoro load ONNX, so the floor is higher than Piper's."""
    return max(60.0, min(600.0, 60.0 + len(text) * 0.2))


def _run_synth(
    cmd: list[str],
    generation: int | None,
    *,
    stdin_text: str | None = None,
    timeout: float | None = None,
    capture_stdout: bool = False,
) -> tuple[int, str, str] | None:
    """Spawn a synth child and wait.

    Returns ``(returncode, stdout, stderr)``. None when speech was cancelled
    before the process was registered. A timeout or other wait error kills
    the child and re-raises so the caller can drop its temp file. Stop can
    also kill the registered process while ``communicate`` is blocked.
    """
    proc = _popen_for_speech(
        cmd,
        generation,
        slot="synth",
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc is None:
        return None
    try:
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
        except Exception:
            # communicate does not reap on timeout. Kill so this thread is
            # not stuck on a child that never exits.
            proc.kill()
            best_effort(lambda: proc.wait(timeout=5.0), "synth proc wait failed")
            raise
        code = proc.returncode if proc.returncode is not None else 1
        return code, str(stdout or ""), str(stderr or "")
    finally:
        _clear_speech_proc(proc, "synth")


def _run_synth_to_temp(
    tmp_wav: str,
    cmd: list[str],
    generation: int | None,
    *,
    label: str,
    stdin_text: str | None = None,
    timeout: float | None = None,
    missing_hint: str | None = None,
) -> tuple[str | None, str]:
    """Run a synth command into *tmp_wav*; return (path, stderr).

    path is None on failure and the temp is released. stderr is returned
    only for an exit-0 run (Kokoro uses it for the G2P fallback notice).
    """
    ok = False
    try:
        result = _run_synth(cmd, generation, stdin_text=stdin_text, timeout=timeout)
        if result is None:
            return None, ""
        code, _stdout, stderr = result
        if code != 0:
            log.warning("%s failed (code %d): %s", label, code, stderr.strip())
            return None, ""
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            ok = True
            return tmp_wav, stderr
        log.warning("%s produced empty audio", label)
        return None, stderr
    except FileNotFoundError as exc:
        if missing_hint:
            log.warning(missing_hint)
        else:
            log.warning("%s execution error: %s", label, exc)
        return None, ""
    except Exception as exc:
        log.warning("%s execution error: %s", label, exc)
        return None, ""
    finally:
        if not ok:
            _release_temp(tmp_wav)


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

    try:
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
            timeout_sec=max(pool.job_timeout_sec, _kokoro_synth_timeout(text)),
            cancel_check=lambda: _playback_blocked(generation),
        )
    except Exception:
        # The pool returns error dicts for spawn and IPC failures. respawn()
        # and an unexpected raise used to skip the CLI and one-shot fallbacks,
        # and this temp stayed tracked until the next stop.
        log.exception("Kokoro warm worker raised; falling back to one-shot synthesis")
        _release_temp(tmp_wav)
        return None, True
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
            _notify_kokoro_g2p_fallback(
                warning,
                misaki_ready,
                on_status,
                g2p=result.get("g2p") if isinstance(result.get("g2p"), str) else None,
            )
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
    """Cold Kokoro load. Used when the warm worker cannot start."""
    if _playback_blocked(generation):
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    lang = kokoro_g2p_lang(text, voice)
    worker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
    cmd = [py_exe, worker_py, "--once", "-", voice, str(speed), tmp_wav, model_path, voices_path, lang]
    ok = False
    try:
        synth_res = _run_synth(
            cmd,
            generation,
            stdin_text=text,
            timeout=_kokoro_synth_timeout(text),
            capture_stdout=True,
        )
        if synth_res is None:
            return None
        code, stdout, stderr = synth_res
        if code != 0:
            log.warning("Venv Kokoro failed (code %d): %s", code, stderr.strip())
            return None

        if not (os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0):
            log.warning("Venv Kokoro produced empty audio")
            return None

        g2p: str | None = None
        warning = ""
        out_str = stdout.strip()
        if out_str:
            try:
                data = json.loads(out_str)
                if isinstance(data, dict):
                    if isinstance(data.get("g2p"), str):
                        g2p = data["g2p"]
                    if isinstance(data.get("warning"), str):
                        warning = data["warning"]
            except Exception:
                log.debug("Kokoro once stdout was not JSON: %s", out_str)

        if warning.strip():
            log.warning("Kokoro: %s", warning.strip())

        if warning.strip() or (misaki_ready is False and g2p != "misaki"):
            _notify_kokoro_g2p_fallback(warning, misaki_ready, on_status, g2p=g2p)

        ok = True
        return tmp_wav
    except Exception as exc:
        log.warning("Venv Kokoro execution error: %s", exc)
        return None
    finally:
        if not ok:
            _release_temp(tmp_wav)


def _kokoro_cli_to_file(
    cand_cli: str,
    text: str,
    voice: str,
    speed: float,
    generation: int | None,
) -> str | None:
    """Kokoro CLI fallback.

    The utterance is the last argv. A leading ``-`` is a flag, and a long
    reply can exceed the OS argument limit. Those cases return None so the
    caller uses the stdin one-shot path. A non-zero exit used to be ignored
    when the file was non-empty, so a partial wav was played.
    """
    if _playback_blocked(generation):
        return None
    if text.startswith("-") or len(text) > _KOKORO_CLI_MAX_CHARS:
        log.info("Kokoro CLI skipped; text is flag-like or too long for argv")
        return None
    tmp_wav = _new_speech_temp(".wav", generation)
    if tmp_wav is None:
        return None
    cmd = [cand_cli, "--voice", voice, "--speed", str(speed), "--output", tmp_wav, text]
    wav_path, _stderr = _run_synth_to_temp(
        tmp_wav, cmd, generation, label="Kokoro CLI", timeout=_kokoro_synth_timeout(text),
    )
    return wav_path


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
    # Bugfix: Model files were downloaded even without a configured venv because
    # resolution ran before checking py_exe. Returning None early skips download
    # and lets sentence mode / caller fall back to OS speech.
    if not py_exe:
        return None
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
    # py_exe is set: an empty venv already returned above, so this is not checked again.
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
    played = False
    if wav_path:
        try:
            played = _play_audio_file(wav_path, generation=generation)
        finally:
            _release_temp(wav_path)
    # `is False` so a test double that returns None is not treated as failure.
    if _playback_blocked(generation) or (wav_path and played is not False):
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

    wav_path, _stderr = _run_synth_to_temp(
        tmp_wav,
        cmd,
        generation,
        label="Piper",
        stdin_text=text,
        timeout=timeout,
        missing_hint="Local Piper executable not found in configured venv. Install via 'uv pip install piper-tts'.",
    )
    return wav_path


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
    played = False
    if wav_path:
        try:
            played = _play_audio_file(wav_path, generation=generation)
        finally:
            _release_temp(wav_path)
    if _playback_blocked(generation) or (wav_path and played is not False):
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
            # Warn once per reply when venv is missing; _kokoro_audio_file does not log.
            if provider == "kokoro" and not _venv_python():
                log.warning(
                    "Local Kokoro TTS requires a configured Python venv. "
                    "Please configure your venv path in Settings -> Python."
                )
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
    try:
        while True:
            clip = ready.get(generation)
            if clip is None:
                break
            try:
                # `is False` so a test double that returns None is not a failure.
                played = False
                if clip.path:
                    played = _play_audio_file(clip.path, generation=generation)
                    if played is False and not _playback_blocked(generation):
                        _speak_system(clip.text, speed=speed, generation=generation)
                elif clip.speak_system:
                    _speak_system(clip.text, speed=speed, generation=generation)
            finally:
                _release_temp(clip.path)
    finally:
        # A raise in playback used to leave tts-prefetch blocked in put()
        # and the queued temps on disk until a later stop bumped the
        # generation. close() shares the queue lock with put(), so nothing
        # can append after it returns.
        ready.close()
        for path in ready.drain():
            _unlink_quiet(path)
        with _speech_lock:
            if _ready_queue is ready:
                _ready_queue = None


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
    log.info("Speaking via endpoint TTS (%s, model=%s, voice=%s)", public_target(endpoint_url), model, voice)
    if _playback_blocked(generation):
        return

    chunks = _endpoint_chunks(text)
    if not chunks:
        return

    def stop_checker() -> bool:
        return _playback_blocked(generation)

    if len(chunks) == 1:
        chunk = chunks[0]
        clip = _download_endpoint_speech(
            chunk,
            endpoint_url,
            api_key,
            model,
            voice,
            speed=speed,
            on_status=on_status,
            stop_checker=stop_checker,
        )
        if _playback_blocked(generation):
            return
        played = False
        if clip:
            tmp_path = _new_speech_temp(clip.suffix, generation)
            if tmp_path is None:
                if not _playback_blocked(generation):
                    _notify_tts_status(_("Could not save speech audio."), on_status)
                played = False
            else:
                try:
                    with open(tmp_path, "wb") as f_out:
                        f_out.write(clip.audio)
                    played = _play_audio_file(tmp_path, generation=generation)
                except OSError:
                    log.exception("Could not write TTS audio to %s", tmp_path)
                    if not _playback_blocked(generation):
                        _notify_tts_status(_("Could not save speech audio."), on_status)
                    played = False
                finally:
                    _release_temp(tmp_path)
        if _playback_blocked(generation):
            return
        if played is False:
            log.warning("Endpoint TTS failed; falling back to OS system speech.")
            _speak_system(chunk, speed=speed, generation=generation)
        return

    # Two or more chunks: pipeline prefetching
    eff_gen = _speech_generation if generation is None else generation
    global _ready_queue
    with _speech_lock:
        if _playback_blocked_locked(generation):
            return
        ready = _ReadyQueue(SPEECH_READY_MAX_CLIPS, SPEECH_READY_MAX_BYTES)
        _ready_queue = ready

    def _produce() -> None:
        try:
            for index, chunk in enumerate(chunks):
                if _playback_blocked(generation):
                    return
                clip = _download_endpoint_speech(
                    chunk,
                    endpoint_url,
                    api_key,
                    model,
                    voice,
                    speed=speed,
                    on_status=on_status,
                    stop_checker=stop_checker,
                )
                if _playback_blocked(generation):
                    return
                if clip is None:
                    remaining = chunks[index:]
                    if not _playback_blocked(generation):
                        ready.put(_ReadyClip(None, " ".join(remaining), 0, True), eff_gen)
                    return
                tmp_path = _new_speech_temp(clip.suffix, generation)
                if tmp_path is None:
                    if not _playback_blocked(generation):
                        _notify_tts_status(_("Could not save speech audio."), on_status)
                    remaining = chunks[index:]
                    if not _playback_blocked(generation):
                        ready.put(_ReadyClip(None, " ".join(remaining), 0, True), eff_gen)
                    return
                try:
                    with open(tmp_path, "wb") as f_out:
                        f_out.write(clip.audio)
                except OSError:
                    log.exception("Could not write TTS audio to %s", tmp_path)
                    _release_temp(tmp_path)
                    if not _playback_blocked(generation):
                        _notify_tts_status(_("Could not save speech audio."), on_status)
                    remaining = chunks[index:]
                    if not _playback_blocked(generation):
                        ready.put(_ReadyClip(None, " ".join(remaining), 0, True), eff_gen)
                    return
                if not ready.put(_ReadyClip(tmp_path, chunk, len(clip.audio), False), eff_gen):
                    _release_temp(tmp_path)
                    return
        except Exception:
            log.exception("TTS endpoint prefetch thread failed")
        finally:
            ready.close()

    run_in_background(_produce, dedicated=True, name="tts-endpoint-prefetch")
    try:
        while True:
            item = ready.get(eff_gen)
            if item is None:
                break
            try:
                played = False
                if item.path:
                    played = _play_audio_file(item.path, generation=generation)
                    if played is False and not _playback_blocked(generation):
                        _speak_system(item.text, speed=speed, generation=generation)
                elif item.speak_system and not _playback_blocked(generation):
                    _speak_system(item.text, speed=speed, generation=generation)
            finally:
                _release_temp(item.path)
    finally:
        ready.close()
        for path in ready.drain():
            _unlink_quiet(path)
        with _speech_lock:
            if _ready_queue is ready:
                _ready_queue = None


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

            # Captured before the worker. The sentence list was split for this
            # provider; reading config again could speak a different one.
            provider_code = clean_provider_name(active_provider)
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
                best_effort(on_complete, "on_complete callback error")

    try:
        run_in_background(_worker, dedicated=True, name="tts-speak")
    except Exception:
        # _begin_utterance has already set _speech_active. Thread.start can
        # raise (RuntimeError at shutdown or when the process cannot create a
        # thread). The worker finally never runs, so is_speaking() stayed true
        # and the sidebar Stop control stayed enabled.
        log.exception("speak_text_async: failed to start speech worker")
        _end_utterance(generation)
        raise
