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
    RecordingStopHandoff,
    make_temp_wav_path,
    monitor_recording_stdout,
    resolve_recording_python,
    spawn_recording_process,
    stop_recording_process,
    terminate_recording_process,
    wait_for_recording_ready,
)
from plugin.scripting.native_binaries import ensure_native_binaries_on_path
from plugin.scripting.audio_silence_detector import SilenceDetector, load_silence_detector_config

log = logging.getLogger(__name__)


def _wav_file_has_bytes(path: str | None) -> bool:
    """True when the capture child has already written a non-empty WAV.

    The child writes the file continuously. A stop handshake can still fail
    after that (the silence monitor used to drop the ``ok`` line), and the
    bytes on disk are the take the user just finished.
    """
    if not path:
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 44
    except OSError:
        return False


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
    ctx: Any
    _auto_stop_lock: threading.Lock
    _wav_lock: threading.Lock
    state: AudioRecorderState
    _test_skip_spawn: bool
    _test_missing_wav: bool
    _test_hang_ready: bool
    _stub_start_count: int

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.temp_filename: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_monitor: threading.Thread | Any = None
        self._stop_handoff: RecordingStopHandoff | None = None
        self._auto_stopped_path: str | None = None
        self._auto_stop_lock = threading.Lock()
        # Held across host writeframes so Stop can close the WAV only after
        # that callback has dropped the file.
        self._wav_lock = threading.Lock()
        self.stream: Any = None
        self.wav_file: Any = None
        self._silence_detector: SilenceDetector | None = None
        self._on_auto_stop: Callable[[], None] | None = None
        self._on_silence_progress: Callable[[int], None] | None = None
        self._on_recording_error: Callable[[str], None] | None = None
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
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Register UI hooks for silence-based auto-stop and child errors."""
        self._on_auto_stop = on_auto_stop
        self._on_silence_progress = on_silence_progress
        self._on_recording_error = on_error

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

    def _notify_recording_error(self, msg: str) -> None:
        """Stdout-monitor hook. Must not raise (ReportErrorEffect raises).

        A child ``error`` after ready used to call ``_apply_event`` on the
        monitor thread. That raised, and StopRecordingEffect then deleted a
        non-empty WAV because status was already ``error``. The panel posts
        ``on_error`` onto the UI thread, the same way silence auto-stop does.
        """
        callback = self._on_recording_error
        if callback is not None:
            try:
                callback(msg)
            except Exception as exc:
                log.debug("Failed to dispatch audio error callback: %s", exc)
            return
        self.apply_stdout_error(msg)

    def apply_stdout_error(self, msg: str) -> None:
        """Apply a post-ready child error without escaping ReportErrorEffect."""
        try:
            self._apply_event(ErrorOccurredEvent(msg))
        except RuntimeError:
            log.warning("audio recorder: %s", msg)

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
        # One handoff shared with stop. The monitor is the only stdout reader
        # after ready; stop waits here instead of reading the same pipe.
        handoff = RecordingStopHandoff()
        self._stop_handoff = handoff
        self._stdout_monitor = monitor_recording_stdout(
            proc,
            on_auto_stopped=lambda path: self._notify_auto_stop(path),
            on_silence_progress=self._notify_silence_progress,
            on_error=self._notify_recording_error,
            handoff=handoff,
        )

    def _cleanup_failed_start(self) -> None:
        terminate_recording_process(self._proc)
        self._proc = None
        self._stdout_monitor = None
        self._stop_handoff = None
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
        self._delete_wav()

    def _keep_recorded_wav_or_cleanup(
        self,
        proc: subprocess.Popen[str] | None,
        auto_path: str | None,
        exc: BaseException,
    ) -> None:
        """Prefer an on-disk WAV over deleting it when stop's handshake fails.

        A lost ``ok`` must not delete the WAV. The stdout monitor and
        ``stop_recording_process`` both read the child pipe, and the monitor
        ignores ``ok``, so the handshake can fail after the child has already
        written the path. A non-empty file is still the recording.
        ``auto_path`` is the same idea for silence auto-stop, which publishes
        the path before ``ok``.
        """
        if auto_path:
            self.temp_filename = auto_path
            terminate_recording_process(proc)
            return
        if _wav_file_has_bytes(self.temp_filename):
            log.warning("Stop handshake failed; using WAV already on disk: %s", exc)
            terminate_recording_process(proc)
            return
        log.debug("Failed to stop recording subprocess: %s", exc)
        # Cleanup terminates ``self._proc``. The stop effect already cleared it,
        # so put the child back or the mic process is leaked and the empty
        # temp file is left behind.
        self._proc = proc
        self._cleanup_failed_start()

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
                    # One-shot: G4 must not leave auto_stop for G5–G15.
                    write_stub_recorder_control(auto_stop=False)
                    # Report auto-stop from a worker, matching the real silence
                    # detector's stdout monitor thread. Firing it inside Record's
                    # start transition posts STOP_REC_CLICKED inline under
                    # WRITERAGENT_TESTING, so the send drain re-enters this
                    # recorder mid-start on the URP Record click and wedges
                    # soffice. The worker post lands on a later VCL tick after
                    # Record has returned.
                    from plugin.framework.worker_pool import run_in_background

                    run_in_background(
                        self._notify_auto_stop,
                        self.temp_filename,
                        name="audio-rec-stub-auto-stop",
                        dedicated=True,
                    )
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
                    ensure_native_binaries_on_path()
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

                    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                        if status:
                            print(status, file=sys.stderr)
                        pcm = bytes(indata)
                        with self._wav_lock:
                            wav = self.wav_file
                            # Status and the file are read together. Stop nulls
                            # wav_file under the same lock before close, so a
                            # chunk cannot pass this check and then write a
                            # closed file.
                            if self.state.status != "recording" or wav is None:
                                return
                            try:
                                wav.writeframes(pcm)
                            except Exception:
                                # Stop may already have closed the WAV.
                                # Swallow the write so the exception does not
                                # abort the PortAudio thread and drop later chunks.
                                log.debug("host recording writeframes failed", exc_info=True)
                                return
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

            # stream.stop() returns only after the current callback does, and
            # that callback no longer holds _wav_lock. Close after that.
            self._close_host_wav()
            self._silence_detector = None

            proc = self._proc
            self._proc = None
            self._stdout_monitor = None
            handoff = self._stop_handoff
            self._stop_handoff = None
            auto_path = self._auto_stopped_path
            self._auto_stopped_path = None

            if proc is not None and self.temp_filename and self.state.status != "error":
                try:
                    if auto_path and proc.poll() is not None:
                        # The child has already exited after auto-stop.
                        # Reap it and drop the stderr drain; updating
                        # temp_filename alone leaves that thread in
                        # _recording_stderr_drains.
                        terminate_recording_process(proc)
                        self.temp_filename = auto_path
                    elif proc.poll() is None:
                        path = stop_recording_process(proc, fallback_path=auto_path, handoff=handoff)
                        self.temp_filename = path
                    else:
                        terminate_recording_process(proc)
                        self.temp_filename = auto_path or self.temp_filename
                except Exception as exc:
                    self._keep_recorded_wav_or_cleanup(proc, auto_path, exc)
            else:
                terminate_recording_process(proc)

            if self.state.status == "error":
                # Failed start still deletes an empty file. A child error
                # after ready hits this same branch, and _cleanup_failed_start
                # used to unlink the WAV the child had already written.
                if not _wav_file_has_bytes(self.temp_filename):
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
        path = self.temp_filename
        self.temp_filename = None
        return path

    def _close_host_wav(self) -> None:
        """Close the host WAV after the capture callback has dropped it.

        Stop must not close wav_file while the PortAudio callback can
        still be inside writeframes, and cleanup's failure path must close
        the file or the mic handle stays open. This waits for _wav_lock,
        clears the attribute the callback checks, then closes.
        """
        with self._wav_lock:
            wav = self.wav_file
            self.wav_file = None
            if wav is None:
                return
            try:
                wav.close()
            except Exception as e:
                log.debug("Failed to close wav_file: %s", e)

    def cleanup(self) -> None:
        """Terminate an in-flight recording child (panel teardown)."""
        if self._proc is not None or self.stream is not None or self.state.status in ("initializing", "recording"):
            try:
                self._apply_event(StopRequestedEvent())
            except Exception:
                # A failed Stop must not leave the host WAV open. The
                # callback can still be inside writeframes, so stop the
                # stream first (that waits for the callback) and close after.
                terminate_recording_process(self._proc)
                self._proc = None
                self._stdout_monitor = None
                self._stop_handoff = None
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
                self._close_host_wav()
                self._silence_detector = None
        self._delete_wav()

    def _delete_wav(self) -> None:
        if self.temp_filename:
            try:
                os.remove(self.temp_filename)
            except OSError as exc:
                log.debug("Failed to remove temp_filename: %s", exc)
            self.temp_filename = None
