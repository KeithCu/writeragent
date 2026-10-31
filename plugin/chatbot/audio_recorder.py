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

import logging
import os
import struct
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.errors import best_effort

from plugin.chatbot.audio_recorder_state import (
    AudioRecorderEvent,
    AudioRecorderState,
    AudioStatus,
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

if TYPE_CHECKING:
    from plugin.framework.managed_subprocess import ManagedSubprocess
    from plugin.framework.worker_pool import BackgroundHandle

log = logging.getLogger(__name__)


WAV_HEADER_BYTES = 44

# Matches plugin/scripting/venv/audio_recorder.py:19
MAX_RECORDING_DURATION_SEC = 4 * 3600


def finalize_wav_header(path: str | None) -> bool:
    """Rewrite RIFF and data chunk sizes for a 44-byte PCM WAV header."""
    if not path or not os.path.isfile(path):
        return False
    try:
        size = os.path.getsize(path)
        if size <= WAV_HEADER_BYTES:
            return False
        data_bytes = size - WAV_HEADER_BYTES
        if data_bytes % 2 != 0:
            return False
        with open(path, "r+b") as f:
            header = f.read(WAV_HEADER_BYTES)
            if len(header) < WAV_HEADER_BYTES:
                return False
            if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return False
            f.seek(4)
            f.write(struct.pack("<I", size - 8))
            f.seek(40)
            f.write(struct.pack("<I", data_bytes))
        return True
    except OSError:
        return False


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


class AudioRecorder:
    fs: int = 16000
    channels: int = 1
    ctx: Any
    _auto_stop_lock: threading.Lock
    _auto_stop_notified: bool
    _wav_lock: threading.Lock
    state: AudioRecorderState

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.temp_filename: str | None = None
        self._proc: ManagedSubprocess | None = None
        self._stdout_monitor: BackgroundHandle | None = None
        self._stop_handoff: RecordingStopHandoff | None = None
        self._auto_stopped_path: str | None = None
        self._auto_stop_notified = False
        self._auto_stop_lock = threading.Lock()
        # Held across host writeframes so Stop can close the WAV only after
        # that callback has dropped the file.
        self._wav_lock = threading.Lock()
        self.stream: Any = None
        self.wav_file: Any = None
        self._silence_detector: SilenceDetector | None = None
        self._host_stream_start_time: float = 0.0
        self._on_auto_stop: Callable[[], None] | None = None
        self._on_silence_progress: Callable[[int], None] | None = None
        self._on_recording_error: Callable[[str], None] | None = None
        self.state = AudioRecorderState(status=AudioStatus.IDLE)

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
        # Bugfix: the host callback runs on every silent chunk once
        # should_stop is true, and each call posted STOP_REC_CLICKED while
        # the UI thread was inside stop. The venv child sets auto_stopped and
        # returns. This flag is the host latch. Do not call the callback or
        # rewrite the path when it is already set.
        with self._auto_stop_lock:
            if self._auto_stop_notified:
                return
            self._auto_stop_notified = True
            if path:
                self._auto_stopped_path = path
            if self._on_auto_stop is None:
                return
            callback = self._on_auto_stop
        log.info("audio recorder: notifying auto-stop (path=%s)", path)
        best_effort(callback, "dispatch audio auto-stop callback")

    def _notify_recording_error(self, msg: str) -> None:
        """Stdout-monitor hook. Must not raise (ReportErrorEffect raises).

        A child ``error`` after ready used to call ``_apply_event`` on the
        monitor thread. That raised, and StopRecordingEffect then deleted a
        non-empty WAV because status was already ``error``. The panel posts
        ``on_error`` onto the UI thread, the same way silence auto-stop does.
        """
        callback = self._on_recording_error
        if callback is not None:
            best_effort(lambda: callback(msg), "dispatch audio error callback")
            return
        self.apply_stdout_error(msg)

    def apply_stdout_error(self, msg: str) -> None:
        """Apply a post-ready child error without escaping ReportErrorEffect."""
        try:
            self._apply_event(ErrorOccurredEvent(msg))
        except RuntimeError:
            log.warning("audio recorder: %s", msg)

    def _notify_silence_progress(self, ms: int) -> None:
        cb = self._on_silence_progress
        if cb is not None:
            best_effort(lambda: cb(ms), "dispatch silence progress callback")

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

    def _take_new_temp_wav(self) -> None:
        # Bugfix:
        # - What was wrong: a child error after ready keeps a non-empty WAV
        #   (_cleanup_failed_start used to unlink it). The next InitializeDeviceEffect
        #   assigned a new mkstemp path over temp_filename, so the old file stayed
        #   in the temp directory.
        # - How: stop_recording is what clears the pointer after the caller takes
        #   the path. The error effect does not. Sidebar cleanup deletes only the
        #   path still stored.
        # - Why this fixes it: the three new-take assignments delete the abandoned
        #   file first. A path already returned to the sidebar is None here.
        self._delete_wav()
        self.temp_filename = make_temp_wav_path()

    def _teardown_capture(self) -> None:
        """Idempotent teardown of child process, stream, and WAV file."""
        if self._proc is not None:
            terminate_recording_process(self._proc, monitor=self._stdout_monitor, handoff=self._stop_handoff)
        self._proc = None
        self._stdout_monitor = None
        self._stop_handoff = None
        self._auto_stopped_path = None
        if self.stream is not None:
            # stream.stop() returns only after the current callback does, and
            # that callback no longer holds _wav_lock. Close after that.
            best_effort(self.stream.stop, "audio stream stop")
            best_effort(self.stream.close, "audio stream close")
            self.stream = None
        self._close_host_wav()
        self._silence_detector = None

    def _cleanup_failed_start(self) -> None:
        self._teardown_capture()
        self._delete_wav()

    def _keep_recorded_wav_or_cleanup(
        self,
        proc: ManagedSubprocess | None,
        auto_path: str | None,
        exc: BaseException,
        *,
        monitor: BackgroundHandle | None = None,
        handoff: RecordingStopHandoff | None = None,
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
            if finalize_wav_header(auto_path):
                self.temp_filename = auto_path
                terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                return
        elif _wav_file_has_bytes(self.temp_filename):
            if finalize_wav_header(self.temp_filename):
                log.warning("Stop handshake failed; using WAV already on disk: %s", exc)
                terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                return
        log.debug("Failed to stop recording subprocess: %s", exc)
        # Cleanup terminates ``self._proc``. The stop effect already cleared it,
        # so put the child back or the mic process is leaked and the empty
        # temp file is left behind.
        self._proc = proc
        self._stdout_monitor = monitor
        self._stop_handoff = handoff
        self._cleanup_failed_start()

    def _execute_effect(self, effect: object) -> None:
        import wave

        if isinstance(effect, InitializeDeviceEffect):
            self._auto_stop_notified = False
            if os.environ.get("WRITERAGENT_TESTING") == "1":
                from plugin.chatbot.sidebar_test_hooks import try_stub_initialize

                if try_stub_initialize(self):
                    return
            silence_config = load_silence_detector_config()
            self._auto_stopped_path = None
            exe, _err = resolve_recording_python()
            if exe:
                try:
                    self._take_new_temp_wav()
                    assert self.temp_filename is not None
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

                    self._take_new_temp_wav()
                    assert self.temp_filename is not None
                    self.wav_file = wave.open(self.temp_filename, "wb")
                    self.wav_file.setnchannels(self.channels)
                    self.wav_file.setsampwidth(2)  # 16-bit
                    self.wav_file.setframerate(self.fs)
                    self._silence_detector = SilenceDetector(silence_config, sample_rate=self.fs)
                    log.info(
                        "audio recorder: host sounddevice path (silence_stop_ms=%d)",
                        silence_config.silence_stop_ms,
                    )

                    self._host_stream_start_time = time.monotonic()

                    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                        if status:
                            log.debug("host recording status: %s", status)
                        if time.monotonic() - self._host_stream_start_time > MAX_RECORDING_DURATION_SEC:
                            self._notify_auto_stop(self.temp_filename)
                            self._notify_recording_error("Recording stopped: recording too long")
                            return
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
                        result = detector.process_chunk(pcm)
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
            if os.environ.get("WRITERAGENT_TESTING") == "1":
                from plugin.chatbot.sidebar_test_hooks import try_stub_stop

                if try_stub_stop(self):
                    return
            if self.stream is not None:
                self._teardown_capture()

            proc = self._proc
            monitor = self._stdout_monitor
            handoff = self._stop_handoff
            self._proc = None
            self._stdout_monitor = None
            self._stop_handoff = None
            auto_path = self._auto_stopped_path
            self._auto_stopped_path = None

            if proc is not None and self.temp_filename and self.state.status != "error":
                try:
                    if auto_path and not proc.is_alive():
                        # The child has already exited after auto-stop.
                        # Reap it and drop the stderr drain; updating
                        # temp_filename alone leaves that thread in
                        # _recording_stderr_drains.
                        terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                        self.temp_filename = auto_path
                    elif proc.is_alive():
                        if handoff is None:
                            # The monitor is the only writer of the handoff.
                            # Building a new one here waits out the stop timeout
                            # and then reaps. No monitor means there is nothing to
                            # wait for.
                            terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                        else:
                            path = stop_recording_process(proc, handoff=handoff, monitor=monitor)
                            self.temp_filename = path
                    else:
                        terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                        self.temp_filename = auto_path or self.temp_filename
                except Exception as exc:
                    self._keep_recorded_wav_or_cleanup(proc, auto_path, exc, monitor=monitor, handoff=handoff)
            else:
                terminate_recording_process(proc, monitor=monitor, handoff=handoff)

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
            if wav is not None:
                best_effort(wav.close, "Failed to close wav_file")

    def cleanup(self) -> None:
        """Terminate an in-flight recording child (panel teardown)."""
        if self._proc is not None or self.stream is not None or self.state.status in ("initializing", "recording"):
            try:
                self._apply_event(StopRequestedEvent())
            except Exception:
                # A failed Stop must not leave the host WAV open. The
                # callback can still be inside writeframes, so stop the
                # stream first (that waits for the callback) and close after.
                self._teardown_capture()
        self._delete_wav()

    def _delete_wav(self) -> None:
        if self.temp_filename:
            try:
                os.remove(self.temp_filename)
            except OSError as exc:
                log.debug("Failed to remove temp_filename: %s", exc)
            self.temp_filename = None
