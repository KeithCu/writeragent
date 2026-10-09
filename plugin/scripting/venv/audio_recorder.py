# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv-side microphone capture via sounddevice (user-installed in Settings → Python venv)."""

from __future__ import annotations

import sys
import threading
import wave
from typing import Any, Callable

from plugin.scripting.audio_silence_detector import SilenceDetector, SilenceDetectorConfig

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM
MAX_RECORDING_DURATION_SEC = 4 * 3600  # Cap recording at 4 hours to avoid runaway processes.


def portaudio_hint() -> str:
    """Platform-specific installation hint for PortAudio."""
    if sys.platform == "darwin":
        return "Audio recording requires PortAudio. On macOS, please run: brew install portaudio"
    if sys.platform == "win32":
        return "Audio recording requires PortAudio. Please install PortAudio DLLs or configure your venv."
    return "Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2"


SOUNDDEVICE_MISSING_HINT = (
    "Install sounddevice in your Python venv: uv pip install sounddevice "
    "(Settings → Python → configure the venv path first)."
)


def _import_sounddevice() -> Any:
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(SOUNDDEVICE_MISSING_HINT) from exc
    except OSError as exc:
        raise RuntimeError(portaudio_hint()) from exc
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

    vad = (
        SilenceDetector(silence_config, sample_rate=SAMPLE_RATE)
        if (silence_config is not None and silence_config.enabled)
        else None
    )
    auto_stopped = False

    # Open the WAV before PortAudio. A disk or permission error must not be
    # reported as PORTAUDIO_LINUX_HINT.
    try:
        wav_file = wave.open(output_path, "wb")
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
    except Exception as exc:
        raise RuntimeError(f"Failed to create audio WAV file at {output_path}: {exc}") from exc

    stream = None

    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        nonlocal auto_stopped
        if status:
            print(status, file=sys.stderr)
        if not wav_file:
            return
        pcm = bytes(indata)
        wav_file.writeframes(pcm)
        if vad is None or auto_stopped:
            return
        result = vad.process_chunk(pcm, frame_count=frames)
        if on_ipc_emit is not None and vad.should_emit_silence_progress(result):
            on_ipc_emit({"status": "silence_progress", "ms": result.silence_ms, "rms": round(result.rms, 5)})
        if result.should_stop:
            auto_stopped = True
            stop_event.set()

    try:
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=callback,
        )
        stream.start()
        if on_stream_started is not None:
            on_stream_started()
        stop_event.wait(timeout=MAX_RECORDING_DURATION_SEC)
    except AssertionError as exc:
        raise RuntimeError(
            "Audio recording is not available on this system (PortAudio backend error)."
        ) from exc
    except OSError as exc:
        raise RuntimeError(portaudio_hint()) from exc
    finally:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if wav_file is not None:
            try:
                wav_file.close()
            except (OSError, ValueError):
                pass

    # Emit auto_stopped on the main thread after the WAV is closed. Emitting
    # from the PortAudio callback publishes a header whose length is still 0.
    if auto_stopped and on_ipc_emit is not None:
        on_ipc_emit({"status": "auto_stopped", "path": output_path})
    return auto_stopped
