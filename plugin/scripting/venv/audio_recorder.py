# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv-side microphone capture via sounddevice (user-installed in Settings → Python venv)."""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import wave
from typing import Any, Callable

from plugin.scripting.audio_silence_detector import (
    RECORDER_STATUS_AUTO_STOPPED,
    RECORDER_STATUS_SILENCE_PROGRESS,
    SilenceDetector,
    SilenceDetectorConfig,
)

SAMPLE_RATE = 16000
CHANNELS = 1
MAX_RECORDING_DURATION_SEC = 4 * 3600  # Cap recording at 4 hours to avoid runaway processes.


log = logging.getLogger(__name__)


def _import_sounddevice() -> Any:
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "Install sounddevice in your Python venv: uv pip install sounddevice "
            "(Settings → Python → configure the venv path first)."
        ) from exc
    except OSError as exc:
        if sys.platform == "darwin":
            hint = "Audio recording requires PortAudio. On macOS, please run: brew install portaudio"
        elif sys.platform == "win32":
            hint = "Audio recording requires PortAudio. Please install PortAudio DLLs or configure your venv."
        else:
            hint = "Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2"
        raise RuntimeError(hint) from exc
    return sd


def record_to_wav(
    output_path: str,
    stop_event: threading.Event,
    *,
    on_stream_started: Callable[[], None] | None = None,
    silence_config: SilenceDetectorConfig | None = None,
    on_ipc_emit: Callable[[dict[str, object]], None] | None = None,
) -> bool:
    """Capture mono 16 kHz PCM to *output_path* until *stop_event* is set.

    Returns True when silence detection triggered the stop (auto-stop), else False.
    """
    sd = _import_sounddevice()

    vad = SilenceDetector(silence_config or SilenceDetectorConfig(silence_stop_ms=0), sample_rate=SAMPLE_RATE)
    auto_stopped = False
    latched: tuple[int, BaseException | None] | None = None

    def note_failure(rank: int, exc: BaseException | None) -> None:
        nonlocal latched
        if latched is None or rank >= latched[0]:
            latched = (rank, exc)

    # Open the WAV before the PortAudio input stream. A disk or permission
    # error then fails with "Failed to create audio WAV file" before the
    # microphone is opened, not as a stream or PortAudio error.
    wav_file: Any = None
    try:
        wav_file = wave.open(output_path, "wb")
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(2)  # 16-bit PCM
        wav_file.setframerate(SAMPLE_RATE)
    except Exception as exc:
        if wav_file is not None:
            with contextlib.suppress(OSError, ValueError):
                wav_file.close()
        raise RuntimeError(f"Failed to create audio WAV file at {output_path}: {exc}") from exc

    # Silence/progress IPC waits until on_stream_started returns so the host never sees a pre-ready line and rejects the recording.
    progress_after_ready = on_stream_started is None

    stream = None

    def on_stream_finished() -> None:
        if not stop_event.is_set():
            note_failure(1, None)
            stop_event.set()

    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        nonlocal auto_stopped
        if status:
            log.debug("audio capture status: %s", status)
        pcm = bytes(indata)
        try:
            wav_file.writeframes(pcm)
        except Exception as exc:
            note_failure(3, exc)
            stop_event.set()
            return
        if auto_stopped:
            return
        try:
            result = vad.process_chunk(pcm)
            if (
                progress_after_ready
                and on_ipc_emit is not None
                and vad.should_emit_silence_progress(result)
            ):
                on_ipc_emit(
                    {"status": RECORDER_STATUS_SILENCE_PROGRESS, "ms": result.silence_ms, "rms": round(result.rms, 5)}
                )
        except Exception as exc:
            note_failure(2, exc)
            stop_event.set()
            return
        if result.should_stop:
            auto_stopped = True
            stop_event.set()

    try:
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=callback,
            finished_callback=on_stream_finished,
        )
        stream.start()
        if on_stream_started is not None:
            on_stream_started()
        progress_after_ready = True
        if not stop_event.wait(timeout=MAX_RECORDING_DURATION_SEC):
            # The 4-hour cap is a normal end of take, not a crash: reuse the
            # auto-stop frame so the host commits the WAV instead of sitting
            # in "recording" forever.
            auto_stopped = True
            stop_event.set()
    except AssertionError as exc:
        raise RuntimeError(
            "Audio recording is not available on this system (PortAudio backend error)."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to start audio capture: {exc}") from exc
    finally:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        with contextlib.suppress(OSError, ValueError):
            wav_file.close()

    # Re-raise latched callback errors after cleanup so the host stops waiting instead of blocking until the recording duration cap.
    if latched is not None:
        rank, latched_exc = latched
        if rank == 3:
            raise RuntimeError(
                f"Failed to write audio WAV file at {output_path}: {latched_exc}"
            ) from latched_exc
        if rank == 2:
            raise RuntimeError(f"Audio capture failed: {latched_exc}") from latched_exc
        raise RuntimeError(
            "Audio capture stopped unexpectedly; the microphone may have been disconnected."
        )

    # Emit auto_stopped on the main thread after the WAV is closed. Emitting
    # from the PortAudio callback publishes a header whose length is still 0.
    if auto_stopped and on_ipc_emit is not None:
        on_ipc_emit({"status": RECORDER_STATUS_AUTO_STOPPED, "path": output_path})
    return auto_stopped
