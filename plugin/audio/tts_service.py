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
from typing import Any, Callable, NamedTuple, cast

from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.audio.kokoro_g2p import (
    G2P_FAILED_MARK,
    KOKORO_PIP_INSTALL,
    ensure_kokoro_misaki,
    kokoro_g2p_lang,
    kokoro_lang_uses_misaki,
)
from plugin.audio.kokoro_pool import (
    get_kokoro_pool,
    resolve_kokoro_python,
)
from plugin.audio.tts_endpoint import download_endpoint_speech
from plugin.audio.tts_models import (
    clear_tts_status,
    notify_tts_status,
    resolve_kokoro_model_files,
    resolve_piper_model_file,
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

# Playback and synthesis overlap during sentence prefetch. Stop kills every
# child tracked here. Each object is the Popen from ManagedSubprocess(raw_spawn=True).
_procs: list[subprocess.Popen[Any]] = []
_speech_active: bool = False
_speech_lock = threading.Lock()
_current: Utterance | None = None

# Six clips is enough that a one-word sentence can be playing while the next
# long sentence (and a few after it) are already synthesized.
SPEECH_READY_MAX_CLIPS = 6
SPEECH_READY_MAX_BYTES = 32 * 1024 * 1024


class Utterance:
    """Per-utterance speech state.

    Groups the cancellation scope, temporary files, prefetch ready queue,
    misaki availability cache, and failed model download tracking for a single
    assistant reply or speech output call.
    """

    def __init__(self, scope: SendCancellation) -> None:
        self.scope: SendCancellation = scope
        self.temps: set[str] = set()
        self.ready: _ReadyQueue | None = None
        # One probe per language per reply, not once per sentence. A cancelled install is not stored.
        self.misaki: dict[str, bool] = {}
        # Remembered per utterance so offline/failed downloads do not retry per sentence.
        self.failed_downloads: set[str] = set()
        self.closed: bool = False

    def note_download_failed(self, key: str) -> None:
        with _speech_lock:
            if self.closed or self is not _current:
                return
            self.failed_downloads.add(key)

    def download_failed(self, key: str) -> bool:
        with _speech_lock:
            return key in self.failed_downloads

    def remember_misaki(self, lang: str, ready: bool) -> None:
        # Stop sets closed under _speech_lock, so a late prefetch write must not
        # record on the utterance that just ended.
        with _speech_lock:
            if self.closed or self is not _current:
                return
            self.misaki[lang] = ready

    def misaki_ready(self, lang: str) -> bool | None:
        with _speech_lock:
            return self.misaki.get(lang)


class _ReadyClip(NamedTuple):
    """One synthesized sentence waiting to play, in utterance order.

    path is None when this sentence is spoken with the OS voice.
    """

    path: str | None
    text: str
    nbytes: int


class _ReadyQueue:
    """Bounded, thread-safe FIFO buffer of synthesized sentence clips."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._cv: threading.Condition = threading.Condition(self._lock)
        self._clips: deque[_ReadyClip] = deque()
        self._closed: bool = False
        self._bytes: int = 0

    def put(self, clip: _ReadyClip, cancel: SendCancellation) -> bool:
        """Enqueue one clip, blocking if full until a clip plays or scope is cancelled."""
        with self._lock:
            while True:
                if is_cancelled(cancel) or self._closed:
                    return False
                if len(self._clips) < 2:
                    break
                if len(self._clips) < SPEECH_READY_MAX_CLIPS and (self._bytes + clip.nbytes) <= SPEECH_READY_MAX_BYTES:
                    break
                self._cv.wait(timeout=0.1)
            self._clips.append(clip)
            self._bytes += clip.nbytes
            self._cv.notify_all()
            return True

    def get(self, cancel: SendCancellation) -> _ReadyClip | None:
        """Pop the next clip in playback order, blocking until synthesized or closed."""
        with self._lock:
            while True:
                if is_cancelled(cancel):
                    return None
                if self._clips:
                    clip = self._clips.popleft()
                    self._bytes = max(0, self._bytes - clip.nbytes)
                    self._cv.notify_all()
                    return clip
                if self._closed:
                    return None
                self._cv.wait(timeout=0.1)

    def close(self) -> None:
        """Mark that no more clips will be produced for this utterance."""
        with self._lock:
            self._closed = True
            self._cv.notify_all()

    def drain(self) -> list[str]:
        """Drop unplayed clips and return their file paths so Stop can unlink them."""
        with self._lock:
            self._closed = True
            paths: list[str] = [clip.path for clip in self._clips if clip.path]
            self._clips.clear()
            self._bytes = 0
            self._cv.notify_all()
            return paths


def _proc_running(proc: subprocess.Popen[Any] | None) -> bool:
    return proc is not None and proc.poll() is None


def _is_spd_say(proc: subprocess.Popen[Any] | None) -> bool:
    args = getattr(proc, "args", None)
    return isinstance(args, (list, tuple)) and bool(args) and os.path.basename(str(args[0])) == "spd-say"


def _signal_stop_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Signal a speech process to stop immediately without blocking the caller.

    Unix sends SIGKILL. Windows kill() is TerminateProcess. terminate()
    and then an immediate kill() never lets a SIGTERM handler run.
    """
    if proc is None:
        return
    if proc.stdin:
        best_effort(proc.stdin.close, "stdin close failed")
    try:
        log.info("Terminating speech process (PID %s)", proc.pid)
        proc.kill()
    except Exception as exc:
        log.debug("speech terminate error: %s", exc)


def _run_quiet(cmd: list[str], timeout: float) -> None:
    ManagedSubprocess.run(
        cmd,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def _reap_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Reap a terminated process in a background thread."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                _run_quiet(["taskkill", "/F", "/T", "/PID", str(proc.pid)], 5)
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
        return _speech_active or any(_proc_running(proc) for proc in _procs)


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


def _cleanup_stopped(
    procs: list[subprocess.Popen[Any]],
    queue_ref: _ReadyQueue | None,
    temps: list[str],
) -> None:
    """Non-blocking signal/kill on caller thread; process reaping, spd-say stop,
    and temporary file unlinking run on the dedicated tts-cleanup background thread."""
    if queue_ref is not None:
        queue_ref.close()

    # Non-blocking signal/kill on current thread
    stop_spd = any(_proc_running(proc) and _is_spd_say(proc) for proc in procs)
    for proc in procs:
        _signal_stop_proc(proc)

    def _bg_cleanup() -> None:
        for proc in procs:
            _reap_proc(proc)
        if stop_spd:
            try:
                _run_quiet(["spd-say", "-S"], 2)
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


def _swap_scope_locked(
    new: Utterance | None,
    active: bool,
) -> tuple[list[subprocess.Popen[Any]], _ReadyQueue | None, list[str]]:
    """Swap the active utterance and drain per-utterance state under _speech_lock."""
    global _speech_active, _current
    old = _current
    queue_ref: _ReadyQueue | None = None
    temps: list[str] = []
    if old is not None:
        old.scope.cancel()
        old.closed = True
        # Do not call note_download_failed here: _speech_lock is already held
        # and is not reentrant.
        queue_ref = old.ready
        old.ready = None
        temps = list(old.temps)
        old.temps.clear()
        # old.misaki dies with the object.
    _current = new
    _speech_active = active
    procs = list(_procs)
    _procs.clear()
    return procs, queue_ref, temps


def stop_speech() -> None:
    """Stop playback, cancel in-flight synthesis, and delete clips not yet played.

    UI-thread safe: sends signals/kills immediately and delegates waiting/reaping,
    spd-say -S, and temp unlinking to a background worker.
    """
    with _speech_lock:
        procs, queue_ref, temps = _swap_scope_locked(None, False)

    _cleanup_stopped(procs, queue_ref, temps)


def _begin_utterance() -> Utterance:
    """Create the utterance, cancel the previous one, and publish the new one under a single lock hold."""
    utt = Utterance(SendCancellation())
    with _speech_lock:
        procs, queue_ref, temps = _swap_scope_locked(utt, True)

    _cleanup_stopped(procs, queue_ref, temps)
    return utt


def _end_utterance(utt: Utterance) -> bool:
    """Tear down per-reply state when the worker thread finishes."""
    global _speech_active
    with _speech_lock:
        if utt is not _current:
            return False
        _speech_active = False
        return True


def _new_speech_temp(suffix: str, cancel: SendCancellation) -> str | None:
    """Create a tracked temp file for audio."""
    with _speech_lock:
        if is_cancelled(cancel):
            return None
        fd, path = tempfile.mkstemp(prefix="writeragent_speech_", suffix=suffix)
        os.close(fd)
        if _current is not None:
            _current.temps.add(path)
        return path


def _release_temp(path: str | None) -> None:
    if not path:
        return
    with _speech_lock:
        if _current is not None:
            _current.temps.discard(path)
    _unlink_quiet(path)


def _popen_for_speech(
    cmd: list[str],
    cancel: SendCancellation,
    *,
    stdin: Any = None,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[Any] | None:
    """Spawn a play or synth process through ManagedSubprocess raw_spawn and return that Popen.

    Does not hold _speech_lock across Popen so is_speaking is not blocked during process startup.
    """
    if is_cancelled(cancel):
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
        label="tts",
    )
    managed.start()
    proc = cast("subprocess.Popen[Any]", managed.popen)
    assert proc is not None

    with _speech_lock:
        if is_cancelled(cancel):
            log.info("Speech cancelled during process spawn, killing process")
            _signal_stop_proc(proc)
            # That reap waits up to 5s and must not take a pool slot.
            run_in_background(lambda: _reap_proc(proc), dedicated=True, name="tts-reap")
            return None
        _procs.append(proc)
        return proc


def _clear_speech_proc(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    with _speech_lock:
        with contextlib.suppress(ValueError):
            _procs.remove(proc)


def sentence_speak_enabled() -> bool:
    """True when audio.tts_sentence_mode is active."""
    return config.get_config("audio.tts_sentence_mode") is True


def uses_sentence_by_sentence(provider: str) -> bool:
    """True only for local Kokoro and Piper."""
    return clean_provider_name(provider) in ("kokoro", "piper")


def sentences_for_speech(text: str, ctx: Any) -> list[str]:
    """Split text into sentences using the BreakIterator sentence splitter."""
    # BreakIterator locale tag: en_US -> en-US.
    locale_key = (get_active_locale() or "en_US").replace("_", "-")
    pairs: list[tuple[int, str]] = []
    try:
        from plugin.writer.locale.grammar_proofread_text import (
            merge_dialogue_sentences,
            split_into_sentences,
        )

        pairs = merge_dialogue_sentences(split_into_sentences(ctx, locale_key, text))
    except Exception:
        log.exception("TTS sentence split failed; speaking the reply as one clip")
    spoken = [chunk.strip() for _offset, chunk in pairs if chunk.strip()]
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
    flags = {
        "ffplay": ["-nodisp", "-autoexit", "-loglevel", "quiet"],
        "mpv": ["--no-video"],
        "mpg123": ["-q"],
    }
    for player in order:
        if not shutil.which(player):
            continue
        cmd = [player, *flags.get(player, []), file_path]
        candidates.append((player, cmd, None))
    return candidates


def _play_audio_file(file_path: str, cancel: SendCancellation) -> bool:
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
        if is_cancelled(cancel):
            return False
        proc: subprocess.Popen[Any] | None = None
        try:
            log.info("Playing audio with command: %s", " ".join(cmd))
            proc = _popen_for_speech(cmd, cancel, env=env)
            if proc is None:
                return False
            ret = proc.wait()
            if ret == 0:
                log.info("Audio playback completed successfully")
                return True
            if is_cancelled(cancel):
                return False
            log.warning("Audio player '%s' exited with code %d; trying next candidate", player_name, ret)
        except Exception as exc:
            log.warning("_play_audio_file playback error with '%s': %s", player_name, exc)
        finally:
            _clear_speech_proc(proc)

    log.warning("All audio players failed for: %s", file_path)
    return False


def _chunk_text(text: str, max_chunk: int = 4000) -> list[str]:
    """Split very long text so one speech request stays under host limits.

    A sentence end in the latter half of the window wins. Otherwise the
    last space, then a hard cut. spd-say and endpoint TTS both use this.
    """
    if len(text) <= max_chunk:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chunk:
        split_idx = _chunk_cut(remaining, max_chunk)
        piece = remaining[:split_idx].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk_cut(text: str, max_chunk: int) -> int:
    """Find the best cut index in text up to max_chunk characters."""
    window = text[:max_chunk]
    split_idx = -1
    for match in _CHUNK_BREAK_RE.finditer(window):
        split_idx = match.end()
    # A break in the first half would make tiny clips and leave the long
    # tail for the next window. Prefer a space near the cap instead.
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
    return [text[:cut].strip()] + _chunk_text(text[cut:].strip(), 4000)


def _speak_system(text: str, speed: float = 1.0, *, cancel: SendCancellation) -> None:
    """Speak text using built-in OS speech synthesis utilities."""
    temp_txt: str | None = None
    try:
        env = None
        stdin_input: str | None = None
        cmd: list[str] | None = None

        if sys.platform in ("darwin", "win32"):
            temp_txt = _new_speech_temp(".txt", cancel)
            if temp_txt is None:
                return
            with open(temp_txt, "w", encoding="utf-8") as handle:
                handle.write(text)
            if sys.platform == "darwin":
                rate = int(175 * speed)
                cmd = ["/usr/bin/say", "-r", str(rate), "-f", temp_txt]
            else:
                rate_int = max(-10, min(10, int((speed - 1.0) * 5)))
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
                    if is_cancelled(cancel):
                        return
                    c_cmd = ["spd-say", "-r", str(rate_pct), "-w", "--", chunk]
                    chunk_proc: subprocess.Popen[Any] | None = None
                    try:
                        chunk_proc = _popen_for_speech(c_cmd, cancel)
                        if chunk_proc is None:
                            return
                        ret = chunk_proc.wait()
                    except OSError as exc:
                        log.warning("_speak_system spd-say OSError: %s", exc)
                        return
                    finally:
                        _clear_speech_proc(chunk_proc)
                    # Bugfix: spd-say failure ignored its exit code and kept speaking remaining chunks.
                    # chunk_proc.wait() was uninspected; now check ret != 0, warn if unblocked, and abort.
                    if ret != 0:
                        if not is_cancelled(cancel):
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
                cancel,
                stdin=subprocess.PIPE if stdin_input is not None else None,
                env=env,
                text=stdin_input is not None,
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
            _clear_speech_proc(proc)
    finally:
        _release_temp(temp_txt)


def _notify_kokoro_g2p_fallback(
    warning: str,
    utt: Utterance,
    lang: str,
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
        notify_tts_status(_("Kokoro used English phonemes for non-English voice"), on_status)
    elif utt.misaki_ready(lang) is False:
        notify_tts_status(_("Misaki not found; Kokoro used English phonemes"), on_status)


def _prepare_kokoro_misaki(
    text: str,
    voice: str,
    on_status: Callable[[str], None] | None,
    utt: Utterance,
    py_exe: str | None = None,
) -> bool | None:
    """Install Misaki at most once per G2P language for this reply."""
    cancel = utt.scope
    lang = kokoro_g2p_lang(text, voice)
    cached = utt.misaki_ready(lang)
    if cached is not None:
        return cached
    if not kokoro_lang_uses_misaki(lang):
        utt.remember_misaki(lang, True)
        return True
    if py_exe is None:
        py_exe = resolve_kokoro_python()
    if not py_exe:
        utt.remember_misaki(lang, False)
        return False

    def _run_misaki_cmd(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        try:
            result = _run_synth(cmd, cancel, timeout=timeout)
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
        on_status=lambda message: notify_tts_status(message, on_status, progress=True),
        cancel=cancel,
        run_cmd=_run_misaki_cmd,
    )
    if ready is None or is_cancelled(cancel):
        return None
    clear_tts_status(on_status)
    utt.remember_misaki(lang, bool(ready))
    return bool(ready)


def _synth_timeout(text: str, floor: float) -> float:
    """Floor is 60s for Kokoro (ONNX load) and 30s for Piper."""
    return max(floor, min(600.0, floor + len(text) * 0.2))


def _run_synth(
    cmd: list[str],
    cancel: SendCancellation,
    *,
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str] | None:
    """Spawn a synth child and wait.

    Returns ``(returncode, stdout, stderr)``. None when speech was cancelled
    before the process was registered. A timeout or other wait error kills
    the child and re-raises so the caller can drop its temp file. Stop can
    also kill the registered process while ``communicate`` is blocked.
    """
    proc = _popen_for_speech(
        cmd,
        cancel,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
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
        _clear_speech_proc(proc)


def _run_synth_to_temp(
    tmp_wav: str,
    cmd: list[str],
    cancel: SendCancellation,
    *,
    label: str,
    stdin_text: str | None = None,
    timeout: float | None = None,
    missing_hint: str | None = None,
) -> tuple[str | None, str]:
    """Run a synth command into *tmp_wav*; return (path, stdout).

    path is None on failure and the temp is released. stdout is returned
    only for an exit-0 run with non-empty audio, otherwise "".
    """
    ok = False
    try:
        result = _run_synth(
            cmd,
            cancel,
            stdin_text=stdin_text,
            timeout=timeout,
        )
        if result is None:
            return None, ""
        code, stdout, stderr = result
        if code != 0:
            log.warning("%s failed (code %d): %s", label, code, stderr.strip())
            return None, ""
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            ok = True
            return tmp_wav, stdout
        log.warning("%s produced empty audio", label)
        return None, ""
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
    utt: Utterance,
    on_status: Callable[[str], None] | None = None,
) -> tuple[str | None, bool]:
    """Synthesize with the keep-alive worker."""
    cancel = utt.scope
    if is_cancelled(cancel):
        return None, False
    try:
        pool = get_kokoro_pool()
    except Exception:
        log.exception("Kokoro warm worker unavailable")
        return None, True
    if pool is None:
        return None, True
    tmp_wav = _new_speech_temp(".wav", cancel)
    if tmp_wav is None:
        return None, False

    lang = kokoro_g2p_lang(text, voice)
    try:
        result = pool.execute(
            {
                "text": text,
                "voice": voice,
                "speed": speed,
                "lang": lang,
                "model_path": model_path,
                "voices_path": voices_path,
                "out_path": tmp_wav,
            },
            timeout_sec=max(pool.job_timeout_sec, _synth_timeout(text, 60.0)),
            cancel=cancel,
        )
    except Exception:
        # The pool returns error dicts for spawn and IPC failures. respawn()
        # and an unexpected raise used to skip the CLI and one-shot fallbacks,
        # and this temp stayed tracked until the next stop.
        log.exception("Kokoro warm worker raised; falling back to one-shot synthesis")
        _release_temp(tmp_wav)
        return None, True
    if is_cancelled(cancel) or result.get("code") == "WORKER_CANCELLED":
        _release_temp(tmp_wav)
        return None, False
    if (
        result.get("status") == "ok"
        and os.path.isfile(tmp_wav)
        and os.path.getsize(tmp_wav) > 0
    ):
        warning = result.get("warning") or ""
        if isinstance(warning, str) and warning.strip():
            _notify_kokoro_g2p_fallback(
                warning,
                utt,
                lang,
                on_status,
                g2p=result.get("g2p") if isinstance(result.get("g2p"), str) else None,
            )
        return tmp_wav, False
    log.warning(
        "Warm Kokoro worker failed (%s); falling back to one-shot synthesis",
        result.get("error"),
    )
    _release_temp(tmp_wav)
    return None, True


def _kokoro_oneshot_to_file(
    py_exe: str,
    text: str,
    voice: str,
    speed: float,
    model_path: str,
    voices_path: str,
    utt: Utterance,
    on_status: Callable[[str], None] | None = None,
) -> str | None:
    """Cold Kokoro load. Used when the warm worker cannot start."""
    cancel = utt.scope
    tmp_wav = _new_speech_temp(".wav", cancel)
    if tmp_wav is None:
        return None
    lang = kokoro_g2p_lang(text, voice)
    worker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
    cmd = [py_exe, worker_py, "--once", "-", voice, str(speed), tmp_wav, model_path, voices_path, lang]
    wav_path, stdout = _run_synth_to_temp(
        tmp_wav,
        cmd,
        cancel,
        label="Venv Kokoro",
        stdin_text=text,
        timeout=_synth_timeout(text, 60.0),
    )
    if wav_path is None:
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
    _notify_kokoro_g2p_fallback(warning, utt, lang, on_status, g2p=g2p)
    return wav_path


def _kokoro_audio_file(
    text: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    utt: Utterance,
) -> str | None:
    """Write one Kokoro wav. Warm worker first, then kokoro_worker.py --once."""
    py_exe = resolve_kokoro_python()
    # Bugfix: Model files were downloaded even without a configured venv because
    # resolution ran before checking py_exe. Returning None early skips download
    # and lets sentence mode / caller fall back to OS speech.
    if not py_exe:
        return None
    model_path, voices_path = resolve_kokoro_model_files(
        on_status=on_status,
        utt=utt,
    )
    if os.path.isfile(model_path) and os.path.isfile(voices_path):
        wav_path, allow_fallback = _kokoro_warm_to_file(
            text, voice, speed, model_path, voices_path, utt,
            on_status=on_status,
        )
        if wav_path or not allow_fallback:
            return wav_path
        return _kokoro_oneshot_to_file(
            py_exe, text, voice, speed, model_path, voices_path, utt,
            on_status=on_status,
        )
    return None


def _wav_played_or_blocked(wav_path: str | None, cancel: SendCancellation) -> bool:
    """True when the caller must not fall back to OS speech."""
    if not wav_path:
        return is_cancelled(cancel)
    try:
        played = _play_audio_file(wav_path, cancel=cancel)
    finally:
        _release_temp(wav_path)
    return bool(is_cancelled(cancel) or played)


def _piper_audio_file(
    text: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    utt: Utterance,
) -> str | None:
    """One Piper wav via CLI. Timeout scales with text length."""
    cancel = utt.scope
    py_exe = resolve_kokoro_python()
    if not py_exe:
        return None
    model_file = resolve_piper_model_file(
        voice,
        on_status=on_status,
        utt=utt,
    )
    if not model_file or not os.path.isfile(model_file):
        return None

    bin_dir = os.path.dirname(py_exe)
    cand_bin = os.path.join(bin_dir, "piper.exe" if sys.platform == "win32" else "piper")
    piper_bin = cand_bin if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK) else None
    tmp_wav = _new_speech_temp(".wav", cancel)
    if tmp_wav is None:
        return None
    length_scale = round(1.0 / max(0.2, min(5.0, speed)), 2)
    head = [piper_bin] if piper_bin else [py_exe, "-m", "piper"]
    cmd = head + ["--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]

    # Generous scaled timeout based on character count
    timeout = _synth_timeout(text, 30.0)

    wav_path, _stdout = _run_synth_to_temp(
        tmp_wav,
        cmd,
        cancel,
        label="Piper",
        stdin_text=text,
        timeout=timeout,
        missing_hint="Local Piper executable not found in configured venv. Install via 'uv pip install piper-tts'.",
    )
    return wav_path


def _speak_local(
    provider: str,
    text: str,
    voice: str,
    speed: float = 1.0,
    on_status: Callable[[str], None] | None = None,
    *,
    utt: Utterance,
) -> None:
    """Synthesize with local Kokoro or Piper and play it."""
    cancel = utt.scope
    engine = "Kokoro" if provider == "kokoro" else "Piper"
    log.info("Speaking via local %s (voice=%s, speed=%.2f)", engine, voice, speed)
    if is_cancelled(cancel):
        return
    py_exe = resolve_kokoro_python()
    if not py_exe:
        log.warning(
            "Local %s TTS requires a configured Python venv. "
            "Please configure your venv path in Settings -> Python.",
            engine,
        )
        _speak_system(text, speed=speed, cancel=cancel)
        return
    if provider == "kokoro":
        lang = kokoro_g2p_lang(text, voice)
        log.info("Kokoro G2P lang=%s for voice=%s", lang, voice)
        if _prepare_kokoro_misaki(text, voice, on_status, utt, py_exe=py_exe) is None:
            return
        wav_path = _kokoro_audio_file(text, voice, speed, on_status, utt)
    else:
        wav_path = _piper_audio_file(text, voice, speed, on_status, utt)
    if _wav_played_or_blocked(wav_path, cancel):
        return
    if provider == "kokoro":
        log.warning(
            "Local Kokoro engine not available in configured venv. "
            "Install with: %s. Falling back to OS speech.",
            KOKORO_PIP_INSTALL,
        )
    else:
        log.warning("Piper synthesis failed for voice %s; falling back to OS speech", voice)
    _speak_system(text, speed=speed, cancel=cancel)


def _synthesize_sentence_clip(
    sentence: str,
    provider: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    utt: Utterance,
) -> _ReadyClip | None:
    """Synthesize one sentence without playing it for sentence prefetch."""
    cancel = utt.scope
    path: str | None = None
    if provider == "kokoro":
        path = _kokoro_audio_file(sentence, voice, speed, on_status, utt)
    elif provider == "piper":
        path = _piper_audio_file(sentence, voice, speed, on_status, utt)

    if is_cancelled(cancel):
        _release_temp(path)
        return None
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        return _ReadyClip(path, sentence, os.path.getsize(path))
    _release_temp(path)
    return _ReadyClip(None, sentence, 0)


def _play_ready_queue(
    ready: _ReadyQueue,
    cancel: SendCancellation,
    speed: float,
) -> None:
    """Play queued clips in order, then drop anything still buffered."""
    try:
        while True:
            clip = ready.get(cancel)
            if clip is None:
                break
            # Stop can win after get() dequeues a clip but before playback starts.
            if is_cancelled(cancel):
                _release_temp(clip.path)
                break
            try:
                played = clip.path and _play_audio_file(clip.path, cancel=cancel)
                if not played and not is_cancelled(cancel):
                    _speak_system(clip.text, speed=speed, cancel=cancel)
            finally:
                _release_temp(clip.path)
    finally:
        for path in ready.drain():
            _unlink_quiet(path)
        with _speech_lock:
            if _current is not None and _current.ready is ready:
                _current.ready = None


def _prefetch(
    chunks: list[str],
    synth: Callable[[int, str], _ReadyClip | None],
    ready: _ReadyQueue,
    cancel: SendCancellation,
    *,
    speak_rest_on_error: bool,
) -> None:
    """Fill the ready queue in order from a per-chunk synthesis closure."""
    done = 0
    try:
        for index, chunk in enumerate(chunks):
            if is_cancelled(cancel):
                return
            clip = synth(index, chunk)
            if clip is None:
                return
            if not ready.put(clip, cancel):
                _release_temp(clip.path)
                return
            done = index + 1
    except Exception:
        log.exception("Prefetch thread failed")
        if speak_rest_on_error:
            rest = chunks[done:]
            if rest and not is_cancelled(cancel):
                ready.put(_ReadyClip(None, " ".join(rest), 0), cancel)


def _run_ready_playback(
    produce: Callable[[_ReadyQueue], None],
    cancel: SendCancellation,
    speed: float,
    *,
    thread_name: str,
) -> None:
    """Register the queue, fill it on a background thread, play it on this thread."""
    ready = _ReadyQueue()
    with _speech_lock:
        if is_cancelled(cancel):
            return
        if _current is not None:
            _current.ready = ready

    def _run() -> None:
        try:
            produce(ready)
        finally:
            ready.close()

    run_in_background(_run, dedicated=True, name=thread_name)
    _play_ready_queue(ready, cancel, speed)


def _run_sentence_pipeline(
    sentences: list[str],
    provider: str,
    voice: str,
    speed: float,
    on_status: Callable[[str], None] | None,
    utt: Utterance,
) -> None:
    """Play sentences in order while synthesis keeps running ahead."""
    cancel = utt.scope
    if provider == "kokoro" and not resolve_kokoro_python():
        # Warn once per reply; _kokoro_audio_file does not log when the venv is missing.
        log.warning(
            "Local Kokoro TTS requires a configured Python venv. "
            "Please configure your venv path in Settings -> Python."
        )

    def _synth(index: int, sentence: str) -> _ReadyClip | None:
        if provider == "kokoro" and _prepare_kokoro_misaki(
            sentence, voice, on_status, utt,
        ) is None:
            return None
        return _synthesize_sentence_clip(
            sentence,
            provider,
            voice,
            speed,
            on_status if index == 0 else None,
            utt,
        )

    def _produce(ready: _ReadyQueue) -> None:
        _prefetch(sentences, _synth, ready, cancel, speak_rest_on_error=True)

    _run_ready_playback(_produce, cancel, speed, thread_name="tts-prefetch")


def _speak_endpoint(
    text: str,
    endpoint_url: str,
    api_key: str,
    model: str = "",
    voice: str = "",
    speed: float = 1.0,
    *,
    cancel: SendCancellation,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Download audio from an OpenAI-compatible speech endpoint and play it."""
    log.info("Speaking via endpoint TTS (%s, model=%s, voice=%s)", public_target(endpoint_url), model, voice)
    chunks = _endpoint_chunks(text)
    if not chunks:
        return

    bailed = False

    def _synth(index: int, chunk: str) -> _ReadyClip | None:
        nonlocal bailed
        if bailed or is_cancelled(cancel):
            return None

        clip = download_endpoint_speech(
            chunk,
            endpoint_url,
            api_key,
            model,
            voice,
            speed=speed,
            on_status=on_status,
            cancel=cancel,
        )
        if is_cancelled(cancel):
            return None

        def _bail(notify: bool = False) -> _ReadyClip:
            nonlocal bailed
            bailed = True
            log.warning("Endpoint TTS failed; falling back to OS system speech.")
            if notify and not is_cancelled(cancel):
                notify_tts_status(_("Could not save speech audio."), on_status)
            return _ReadyClip(None, " ".join(chunks[index:]), 0)

        if clip is None:
            return _bail(notify=False)
        tmp_path = _new_speech_temp(clip.suffix, cancel)
        if tmp_path is None:
            return _bail(notify=True)
        try:
            with open(tmp_path, "wb") as f_out:
                f_out.write(clip.audio)
        except OSError:
            log.exception("Could not write TTS audio to %s", tmp_path)
            _release_temp(tmp_path)
            return _bail(notify=True)
        return _ReadyClip(tmp_path, chunk, len(clip.audio))

    def _produce(ready: _ReadyQueue) -> None:
        _prefetch(chunks, _synth, ready, cancel, speak_rest_on_error=False)

    _run_ready_playback(_produce, cancel, speed, thread_name="tts-endpoint-prefetch")


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
    tts_on = bool(config.get_config("audio.tts_enabled")) if enabled is None else bool(enabled)
    if not tts_on:
        log.debug("speak_text_async: TTS is disabled (audio.tts_enabled=False)")
        return

    clean = clean_text_for_speech(text)
    if not clean:
        log.debug("speak_text_async: No speakable text after cleaning")
        return

    active_provider = provider if provider else str(config.get_config("audio.tts_provider") or "system")
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

    utt = _begin_utterance()
    cancel = utt.scope
    log.info(
        "speak_text_async: queued speech for %d chars (%s)",
        len(clean),
        f"{len(sentences)} sentences" if sentences is not None else "one clip",
    )

    def _worker() -> None:
        try:
            if is_cancelled(cancel):
                return

            # Captured before the worker. The sentence list was split for this
            # provider; reading config again could speak a different one.
            provider_code = clean_provider_name(active_provider)
            speed_val = parse_tts_speed(
                speed if speed is not None else config.get_config("audio.tts_speed")
            )
            model_name = model.strip() if isinstance(model, str) else ""
            if provider_code == "endpoint" and not model_name:
                from plugin.framework.client.model_fetcher import get_tts_model

                model_name = get_tts_model() or "hexgrad/Kokoro-82M"

            if voice and voice.strip():
                voice_name = clean_voice_name(voice)
            else:
                voice_name = get_scoped_tts_voice(provider_code, model_name)

            log.info(
                "TTS worker executing: provider=%s, speed=%.2f, voice=%s, model=%s",
                provider_code,
                speed_val,
                voice_name,
                model_name,
            )

            if sentences is not None:
                _run_sentence_pipeline(
                    sentences,
                    provider_code,
                    voice_name,
                    speed_val,
                    on_status,
                    utt,
                )
            elif provider_code in ("kokoro", "piper"):
                _speak_local(
                    provider_code, clean, voice=voice_name, speed=speed_val,
                    on_status=on_status, utt=utt,
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
                    cancel=cancel,
                    on_status=on_status,
                )
            else:
                _speak_system(clean, speed=speed_val, cancel=cancel)
        except Exception as exc:
            log.exception("speak_text_async worker error: %s", exc)
        finally:
            if _end_utterance(utt) and on_complete:
                best_effort(on_complete, "on_complete callback error")

    try:
        run_in_background(_worker, dedicated=True, name="tts-speak")
    except Exception:
        # _begin_utterance has already set _speech_active. Thread.start can
        # raise (RuntimeError at shutdown or when the process cannot create a
        # thread). The worker finally never runs, so is_speaking() stayed true
        # and the sidebar Stop control stayed enabled.
        log.exception("speak_text_async: failed to start speech worker")
        _end_utterance(utt)
        raise
