# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Host adapter: spawns user-venv recording subprocess for sidebar capture."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import subprocess

from plugin.chatbot.audio_recorder_state import (
    AudioRecorderEvent,
    AudioRecorderState,
    DeviceReadyEvent,
    ErrorOccurredEvent,
    InitializeDeviceEffect,
    ReportErrorEffect,
    StartRecordingEffect,
    StartRequestedEvent,
    StopRecordingEffect,
    StopRequestedEvent,
    next_state,
)
from plugin.scripting.audio_recorder_service import (
    make_temp_wav_path,
    resolve_recording_python,
    spawn_recording_process,
    stop_recording_process,
    terminate_recording_process,
    wait_for_recording_ready,
    ensure_downloaded_audio_on_path,
)

log = logging.getLogger(__name__)


class AudioRecorder:
    fs = 16000
    channels = 1

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.temp_filename: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self.stream: Any = None
        self.wav_file: Any = None
        self.state = AudioRecorderState(status="idle")

    def _cleanup_failed_start(self) -> None:
        terminate_recording_process(self._proc)
        self._proc = None
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.wav_file is not None:
            try:
                self.wav_file.close()
            except Exception:
                pass
            self.wav_file = None
        if self.temp_filename:
            try:
                os.remove(self.temp_filename)
            except OSError as exc:
                log.debug("Failed to remove temp_filename during cleanup: %s", exc)
            self.temp_filename = None

    def _execute_effect(self, effect: object) -> None:
        import sys
        import wave
        if isinstance(effect, InitializeDeviceEffect):
            exe, err = resolve_recording_python(self.ctx)
            if exe:
                try:
                    self.temp_filename = make_temp_wav_path()
                    self._proc = spawn_recording_process(exe, self.temp_filename)
                    wait_for_recording_ready(self._proc)
                    self._apply_event(DeviceReadyEvent())
                except RuntimeError as exc:
                    self._apply_event(ErrorOccurredEvent(str(exc)))
                except Exception as exc:
                    self._apply_event(ErrorOccurredEvent(f"Venv audio recording failed to start: {exc}"))
            else:
                try:
                    ensure_downloaded_audio_on_path()
                    import sounddevice as sd
                    self.temp_filename = make_temp_wav_path()
                    self.wav_file = wave.open(self.temp_filename, "wb")
                    self.wav_file.setnchannels(self.channels)
                    self.wav_file.setsampwidth(2)  # 16-bit
                    self.wav_file.setframerate(self.fs)

                    def callback(indata, frames, time_info, status):
                        if status:
                            print(status, file=sys.stderr)
                        if self.state.status == "recording" and self.wav_file:
                            self.wav_file.writeframes(indata)

                    self.stream = sd.RawInputStream(
                        samplerate=self.fs, channels=self.channels, dtype="int16", callback=callback
                    )
                    self._apply_event(DeviceReadyEvent())
                except Exception as exc:
                    self._apply_event(ErrorOccurredEvent(
                        f"Audio recording failed to start. "
                        f"Please configure a Python venv or click 'Download Audio' in Settings → Python. Error: {exc}"
                    ))

        elif isinstance(effect, StartRecordingEffect):
            if self.stream is not None:
                try:
                    self.stream.start()
                except Exception as e:
                    self._apply_event(ErrorOccurredEvent(f"Audio recording failed to start stream: {e}"))

        elif isinstance(effect, StopRecordingEffect):
            if self.stream is not None:
                try:
                    self.stream.stop()
                except Exception as e:
                    log.debug("Failed to stop stream on StopRecordingEffect: %s", e)
                try:
                    self.stream.close()
                except Exception as e:
                    log.debug("Failed to close stream on StopRecordingEffect: %s", e)
                self.stream = None

            if self.wav_file is not None:
                try:
                    self.wav_file.close()
                except Exception as e:
                    log.debug("Failed to close wav_file on StopRecordingEffect: %s", e)
                self.wav_file = None

            proc = self._proc
            self._proc = None
            if proc is not None and self.temp_filename and self.state.status != "error":
                try:
                    path = stop_recording_process(proc)
                    self.temp_filename = path
                except RuntimeError as exc:
                    log.debug("Failed to stop recording subprocess: %s", exc)
                    self._cleanup_failed_start()
                except Exception as exc:
                    log.debug("Unexpected error stopping recording subprocess: %s", exc)
                    self._cleanup_failed_start()
            else:
                terminate_recording_process(proc)

            if self.state.status == "error":
                self._cleanup_failed_start()

        elif isinstance(effect, ReportErrorEffect):
            raise RuntimeError(effect.error_message)

    def _apply_event(self, event: AudioRecorderEvent) -> None:
        step = next_state(self.state, event)
        self.state = step.state
        for effect in step.effects:
            self._execute_effect(effect)

    def start_recording(self) -> None:
        self._apply_event(StartRequestedEvent())

    def stop_recording(self) -> str | None:
        self._apply_event(StopRequestedEvent())
        return self.temp_filename

    def cleanup(self) -> None:
        """Terminate an in-flight recording child (panel teardown)."""
        if self._proc is not None or self.stream is not None or self.state.status in ("initializing", "recording"):
            try:
                self._apply_event(StopRequestedEvent())
            except Exception:
                terminate_recording_process(self._proc)
                self._proc = None
                if self.stream is not None:
                    try:
                        self.stream.stop()
                    except Exception:
                        pass
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = None
