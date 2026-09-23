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
"""Host adapter: venv subprocess or downloaded sounddevice capture for sidebar recording."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

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
    ensure_downloaded_audio_on_path,
    make_temp_wav_path,
    monitor_recording_stdout,
    resolve_recording_python,
    spawn_recording_process,
    stop_recording_process,
    terminate_recording_process,
    wait_for_recording_ready,
)
from plugin.scripting.audio_silence_detector import SilenceDetector, load_silence_detector_config

log = logging.getLogger(__name__)


def stub_recorder_control_path() -> str:
    """Cross-process Packet G control file (URP tests vs soffice OXT)."""
    return os.path.join(tempfile.gettempdir(), "writeragent_stub_recorder.json")


def read_stub_recorder_control() -> dict[str, Any]:
    path = stub_recorder_control_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_stub_recorder_control(**fields: Any) -> None:
    path = stub_recorder_control_path()
    data = read_stub_recorder_control()
    data.update(fields)
    data["skip"] = True
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def clear_stub_recorder_control() -> None:
    try:
        os.remove(stub_recorder_control_path())
    except OSError:
        pass


class AudioRecorder:
    fs: int = 16000
    channels: int = 1

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.temp_filename: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_monitor: threading.Thread | Any = None
        self._auto_stopped_path: str | None = None
        self._auto_stop_lock = threading.Lock()
        self.stream: Any = None
        self.wav_file: Any = None
        self._silence_detector: SilenceDetector | None = None
        self._on_auto_stop: Callable[[], None] | None = None
        self._on_silence_progress: Callable[[int], None] | None = None
        self.state = AudioRecorderState(status="idle")
        # Packet G native tests: skip venv/PortAudio spawn and use inject_wav.
        self._test_skip_spawn = False
        self._test_inject_wav: str | bytes | None = None
        self._test_fail_start: str | None = None
        self._test_missing_wav = False
        self._test_hang_ready = False
        self._stub_start_count = 0

    def set_auto_stop_callbacks(
        self,
        *,
        on_auto_stop: Callable[[], None] | None = None,
        on_silence_progress: Callable[[int], None] | None = None,
    ) -> None:
        """Register UI hooks for silence-based auto-stop (venv and host capture)."""
        self._on_auto_stop = on_auto_stop
        self._on_silence_progress = on_silence_progress

    def _notify_auto_stop(self, path: str | None = None) -> None:
        with self._auto_stop_lock:
            if path:
                self._auto_stopped_path = path
            if self._on_auto_stop is None:
                return
            callback = self._on_auto_stop
        log.info("audio recorder: notifying auto-stop (path=%s)", path)
        try:
            callback()
        except Exception as exc:
            log.debug("Failed to dispatch audio auto-stop callback: %s", exc)

    def _notify_silence_progress(self, ms: int) -> None:
        if self._on_silence_progress is None:
            return
        try:
            self._on_silence_progress(ms)
        except Exception as exc:
            log.debug("Failed to dispatch silence progress callback: %s", exc)

    def _start_stdout_monitor(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._stdout_monitor = monitor_recording_stdout(
            proc,
            on_auto_stopped=lambda path: self._notify_auto_stop(path),
            on_silence_progress=self._notify_silence_progress,
            on_error=lambda msg: self._apply_event(ErrorOccurredEvent(msg)),
        )

    def _cleanup_failed_start(self) -> None:
        terminate_recording_process(self._proc)
        self._proc = None
        self._stdout_monitor = None
        self._auto_stopped_path = None
        self._silence_detector = None
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

    def _write_injected_wav(self) -> None:
        inject = self._test_inject_wav
        dest = self.temp_filename
        if not dest or inject is None:
            return
        if isinstance(inject, (bytes, bytearray)):
            with open(dest, "wb") as handle:
                handle.write(inject)
            return
        import shutil

        shutil.copyfile(str(inject), dest)

    def _execute_effect(self, effect: object) -> None:
        import sys
        import wave

        if isinstance(effect, InitializeDeviceEffect):
            ctrl = read_stub_recorder_control()
            if ctrl.get("skip"):
                self._test_skip_spawn = True
                # Always apply JSON (including null/false) so G12 fail_start / G14
                # missing_wav cannot stick on the live recorder for later cases.
                fail = ctrl.get("fail_start")
                self._test_fail_start = str(fail) if fail else None
                self._test_missing_wav = bool(ctrl.get("missing_wav"))
                self._test_hang_ready = bool(ctrl.get("hang_ready"))
                wav = ctrl.get("wav")
                if wav:
                    self._test_inject_wav = wav
            if self._test_skip_spawn:
                # Packet G: pretend the capture child said {"status":"ready"} without a mic.
                self._stub_start_count += 1
                self._auto_stopped_path = None
                if self._test_fail_start:
                    self._apply_event(ErrorOccurredEvent(self._test_fail_start))
                    return
                if self._test_hang_ready:
                    # G21: child never emits ready. Do not wait the real 30s spawn timeout.
                    timeout = ctrl.get("ready_timeout_sec")
                    try:
                        timeout_s = float(timeout) if timeout is not None else 0.0
                    except (TypeError, ValueError):
                        timeout_s = 0.0
                    if timeout_s > 0:
                        time.sleep(timeout_s)
                    # Same wording as wait_for_recording_ready TimeoutExpired.
                    shown = timeout_s if timeout_s > 0 else 30
                    self._apply_event(
                        ErrorOccurredEvent(f"Recording subprocess timed out after {shown:g} seconds.")
                    )
                    return
                self.temp_filename = make_temp_wav_path()
                self._proc = None
                self._apply_event(DeviceReadyEvent())
                if ctrl.get("auto_stop"):
                    self._write_injected_wav()
                    self._notify_auto_stop(self.temp_filename)
                    # One-shot: G4 must not leave auto_stop for G5–G15.
                    write_stub_recorder_control(auto_stop=False)
                return
            silence_config = load_silence_detector_config()
            self._auto_stopped_path = None
            exe, _err = resolve_recording_python(self.ctx)
            if exe:
                try:
                    self.temp_filename = make_temp_wav_path()
                    self._proc = spawn_recording_process(exe, self.temp_filename, silence_config=silence_config)
                    log.info(
                        "audio recorder: venv subprocess path (silence_stop_ms=%d)",
                        silence_config.silence_stop_ms,
                    )
                    wait_for_recording_ready(self._proc)
                    self._start_stdout_monitor()
                    self._apply_event(DeviceReadyEvent())
                except RuntimeError as exc:
                    self._apply_event(ErrorOccurredEvent(str(exc)))
                except Exception as exc:
                    self._apply_event(ErrorOccurredEvent(f"Venv audio recording failed to start: {exc}"))
            else:
                # Host-side capture via downloaded sounddevice binaries (no venv).
                try:
                    ensure_downloaded_audio_on_path()
                    import sounddevice as sd

                    self.temp_filename = make_temp_wav_path()
                    self.wav_file = wave.open(self.temp_filename, "wb")
                    self.wav_file.setnchannels(self.channels)
                    self.wav_file.setsampwidth(2)  # 16-bit
                    self.wav_file.setframerate(self.fs)
                    self._silence_detector = SilenceDetector(silence_config, sample_rate=self.fs)
                    log.info(
                        "audio recorder: host sounddevice path (silence_stop_ms=%d)",
                        silence_config.silence_stop_ms,
                    )

                    def callback(indata, frames, time_info, status):
                        if status:
                            print(status, file=sys.stderr)
                        if self.state.status != "recording" or not self.wav_file:
                            return
                        pcm = bytes(indata)
                        self.wav_file.writeframes(pcm)
                        detector = self._silence_detector
                        if detector is None or not silence_config.enabled:
                            return
                        result = detector.process_chunk(pcm, frame_count=frames)
                        if detector.should_emit_silence_progress(result):
                            self._notify_silence_progress(result.silence_ms)
                        if result.should_stop:
                            self._notify_auto_stop(self.temp_filename)

                    self.stream = sd.RawInputStream(
                        samplerate=self.fs, channels=self.channels, dtype="int16", callback=callback
                    )
                    self._apply_event(DeviceReadyEvent())
                except Exception as exc:
                    self._apply_event(
                        ErrorOccurredEvent(
                            f"Audio recording failed to start. "
                            f"Please configure a Python venv or click 'Download Audio' in Settings → Python. Error: {exc}"
                        )
                    )

        elif isinstance(effect, StartRecordingEffect):
            if self.stream is not None:
                try:
                    self.stream.start()
                except Exception as e:
                    self._apply_event(ErrorOccurredEvent(f"Audio recording failed to start stream: {e}"))

        elif isinstance(effect, StopRecordingEffect):
            if self._test_skip_spawn:
                if self._test_missing_wav:
                    if self.temp_filename:
                        try:
                            os.remove(self.temp_filename)
                        except OSError:
                            pass
                    self.temp_filename = None
                else:
                    self._write_injected_wav()
                self._proc = None
                self.stream = None
                self.wav_file = None
                self._silence_detector = None
                return
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
            self._silence_detector = None

            proc = self._proc
            self._proc = None
            self._stdout_monitor = None
            auto_path = self._auto_stopped_path
            self._auto_stopped_path = None

            if proc is not None and self.temp_filename and self.state.status != "error":
                try:
                    if auto_path and proc.poll() is not None:
                        self.temp_filename = auto_path
                    elif proc.poll() is None:
                        path = stop_recording_process(proc, fallback_path=auto_path)
                        self.temp_filename = path
                    else:
                        self.temp_filename = auto_path or self.temp_filename
                except RuntimeError as exc:
                    if auto_path:
                        self.temp_filename = auto_path
                    else:
                        log.debug("Failed to stop recording subprocess: %s", exc)
                        self._cleanup_failed_start()
                except Exception as exc:
                    if auto_path:
                        self.temp_filename = auto_path
                    else:
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
                self._stdout_monitor = None
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
