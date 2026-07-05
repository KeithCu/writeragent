# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv-side microphone capture via sounddevice (user-installed in Settings → Python venv)."""

from __future__ import annotations

import sys
import threading
import wave
from typing import Callable

from plugin.scripting.audio_silence_detector import SilenceDetector, SilenceDetectorConfig

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM

PORTAUDIO_LINUX_HINT = (
    "Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2"
)
SOUNDDEVICE_MISSING_HINT = (
    "Install sounddevice in your Python venv: uv pip install sounddevice "
    "(Settings → Python → configure the venv path first)."
)


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(SOUNDDEVICE_MISSING_HINT) from exc
    except OSError as exc:
        raise RuntimeError(PORTAUDIO_LINUX_HINT) from exc
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

    recording = threading.Event()
    recording.set()
    vad = SilenceDetector(silence_config or SilenceDetectorConfig(silence_stop_ms=0), sample_rate=SAMPLE_RATE)
    auto_stopped = False

    wav_file = wave.open(output_path, "wb")
    wav_file.setnchannels(CHANNELS)
    wav_file.setsampwidth(SAMPLE_WIDTH)
    wav_file.setframerate(SAMPLE_RATE)

    def callback(indata, frames, time_info, status):
        nonlocal auto_stopped
        if status:
            print(status, file=sys.stderr)
        if not recording.is_set() or not wav_file:
            return
        pcm = bytes(indata)
        wav_file.writeframes(pcm)
        if not silence_config or not silence_config.enabled or auto_stopped:
            return
        result = vad.process_chunk(pcm, frame_count=frames)
        if on_ipc_emit is not None and vad.should_emit_silence_progress(result):
            on_ipc_emit({"status": "silence_progress", "ms": result.silence_ms, "rms": round(result.rms, 5)})
        if result.should_stop:
            auto_stopped = True
            if on_ipc_emit is not None:
                on_ipc_emit({"status": "auto_stopped", "path": output_path})
            stop_event.set()

    stream = None
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
        stop_event.wait()
    except AssertionError as exc:
        raise RuntimeError(
            "Audio recording is not available on this system (PortAudio backend error)."
        ) from exc
    except OSError as exc:
        raise RuntimeError(PORTAUDIO_LINUX_HINT) from exc
    finally:
        recording.clear()
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            wav_file.close()
        except (OSError, ValueError):
            pass
    return auto_stopped
