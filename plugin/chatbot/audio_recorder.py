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
from plugin.framework.managed_subprocess import kill_process_tree
from plugin.framework.worker_pool import BackgroundHandle, run_in_background
from plugin.scripting.audio_recorder_service import (
    RecordingStopHandoff,
    finish_recording_stop,
    make_temp_wav_path,
    monitor_recording_stdout,
    resolve_recording_python,
    signal_recording_stop,
    spawn_recording_process,
    terminate_recording_process,
    wait_for_recording_ready,
)
from plugin.scripting.native_binaries import ensure_native_binaries_on_path
from plugin.scripting.audio_silence_detector import SilenceDetector, load_silence_detector_config

if TYPE_CHECKING:
    from plugin.framework.managed_subprocess import ManagedSubprocess

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
    after handshake failure, and on-disk bytes still represent the user's take.
    """
    if not path:
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > WAV_HEADER_BYTES
    except OSError:
        return False


class AudioRecorder:
    fs: int = 16000
    channels: int = 1
    _auto_stop_lock: threading.Lock
    _auto_stop_notified: bool
    _wav_lock: threading.Lock
    _lifecycle_lock: threading.Lock
    stop_deferred: bool
    state: AudioRecorderState

    def __init__(self) -> None:
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
        self._lifecycle_lock = threading.Lock()
        self.stop_deferred = False
        self._epoch: int = 0
        self._start_job: BackgroundHandle | None = None
        self._stop_job: BackgroundHandle | None = None
        self._commit_pending: bool = False
        self._discard_take: bool = False
        self.stream: Any = None
        self.wav_file: Any = None
        self._silence_detector: SilenceDetector | None = None
        self._host_stream_start_time: float = 0.0
        self._on_auto_stop: Callable[[], None] | None = None
        self._on_silence_progress: Callable[[int], None] | None = None
        self._on_recording_error: Callable[[str], None] | None = None
        self._on_device_ready: Callable[[], None] | None = None
        self._on_capture_stopped: Callable[[str | None], None] | None = None
        self.state = AudioRecorderState(status=AudioStatus.IDLE)

    def set_auto_stop_callbacks(
        self,
        *,
        on_auto_stop: Callable[[], None] | None = None,
        on_silence_progress: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_device_ready: Callable[[], None] | None = None,
        on_capture_stopped: Callable[[str | None], None] | None = None,
    ) -> None:
        """Register UI hooks for silence-based auto-stop, child errors, device ready, and stop handoff."""
        self._on_auto_stop = on_auto_stop
        self._on_silence_progress = on_silence_progress
        self._on_recording_error = on_error
        self._on_device_ready = on_device_ready
        self._on_capture_stopped = on_capture_stopped

    def apply_device_ready(self) -> None:
        """Transition from initializing to recording once the device is open.

        No-op unless status is currently initializing.
        """
        if self.state.status == AudioStatus.INITIALIZING:
            self._apply_event(DeviceReadyEvent())

    def _notify_auto_stop(self, path: str | None = None) -> None:
        # _auto_stop_notified latches one STOP_REC_CLICKED per auto-stop; silent chunks must not re-post.
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

        Child errors after ready post on_error on the UI thread, not _apply_event from the monitor.
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

    def _take_new_temp_wav(self) -> str:
        # New takes delete any abandoned temp_filename first so error paths do not leak WAV files.
        self._delete_wav()
        path = make_temp_wav_path()
        self.temp_filename = path
        return path

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

    def _notify_device_ready(self) -> None:
        if self._on_device_ready is not None:
            self._on_device_ready()
        else:
            self.apply_device_ready()

    def _background_rec_stop(self, target: Any, *args: Any, **kwargs: Any) -> BackgroundHandle:
        return run_in_background(
            target,
            *args,
            name="audio-rec-stop",
            dedicated=True,
            daemon=True,
            **kwargs,
        )

    def _if_start_epoch_current(self, epoch: int, assign: Callable[[], None]) -> bool:
        """Run assign under the lifecycle lock when epoch still matches; else stale."""
        with self._lifecycle_lock:
            if self._epoch != epoch:
                return False
            assign()
            return True

    def _finish_stop(
        self,
        proc: ManagedSubprocess,
        handoff: RecordingStopHandoff | None,
        monitor: BackgroundHandle | None,
        auto_path: str | None,
        temp_path: str | None,
    ) -> None:
        """Wait for WAV path and reap child process off the UI thread.

        Handoff wait and child reap run off the VCL thread; on_capture_stopped delivers the path.
        Valid on-disk audio is kept when handshake or reap fails.
        """
        path: str | None = None
        try:
            if handoff is not None:
                try:
                    path = finish_recording_stop(proc, handoff=handoff, monitor=monitor)
                except Exception as exc:
                    log.warning("finish_recording_stop failed: %s", exc)
            if path is None:
                wav_candidate = auto_path or temp_path
                finalized = finalize_wav_header(wav_candidate) if wav_candidate else False
                terminate_recording_process(proc, monitor=monitor, handoff=handoff)
                if finalized and wav_candidate:
                    if not auto_path:
                        log.warning("Stop handshake failed; using WAV already on disk")
                    path = wav_candidate
                else:
                    if temp_path:
                        best_effort(lambda: os.remove(temp_path), "delete failed stop wav")
                    path = None
        finally:
            with self._lifecycle_lock:
                self._commit_pending = False
                discard = self._discard_take
            if discard:
                if path:
                    best_effort(lambda: os.remove(path), "discard captured wav")
            else:
                callback = self._on_capture_stopped
                if callback is not None:
                    best_effort(lambda: callback(path), "on_capture_stopped")

    def _handle_stale_start_exit(
        self,
        temp_path: str | None = None,
        proc: ManagedSubprocess | None = None,
    ) -> None:
        """Clean up resources when start_capture exits early due to cancellation, stop, or error.

        Reads and clears commit_pending under the lifecycle lock, terminates the process,
        deletes the temp WAV, and notifies on_capture_stopped(None) if a commit was pending
        and take was not discarded.
        """
        with self._lifecycle_lock:
            commit = self._commit_pending
            self._commit_pending = False
            discard = self._discard_take
            reap_proc = self._proc
            self._proc = None

        target_proc = proc or reap_proc
        if target_proc is not None:
            terminate_recording_process(target_proc)

        if temp_path:
            best_effort(lambda: os.remove(temp_path), "delete stale start wav")

        self._close_host_wav()

        if commit and not discard:
            callback = self._on_capture_stopped
            if callback is not None:
                best_effort(lambda: callback(None), "on_capture_stopped")

    def _start_capture(self, epoch: int) -> None:
        import wave

        silence_config = load_silence_detector_config()
        self._auto_stopped_path = None
        temp_path: str | None = None
        exe, _err = resolve_recording_python()
        if exe:
            proc: ManagedSubprocess | None = None
            try:
                temp_path = self._take_new_temp_wav()
                proc = spawn_recording_process(exe, temp_path, silence_config=silence_config)
                if not self._if_start_epoch_current(epoch, lambda: setattr(self, "_proc", proc)):
                    self._handle_stale_start_exit(temp_path, proc)
                    return

                log.info(
                    "audio recorder: venv subprocess path (silence_stop_ms=%d)",
                    silence_config.silence_stop_ms,
                )
                wait_for_recording_ready(proc)
                if not self._if_start_epoch_current(epoch, self._start_stdout_monitor):
                    self._handle_stale_start_exit(temp_path, proc)
                    return

                self._notify_device_ready()
            except Exception as exc:
                with self._lifecycle_lock:
                    stale = (self._epoch != epoch)
                self._handle_stale_start_exit(temp_path, proc)
                if not stale:
                    msg = str(exc) if isinstance(exc, RuntimeError) else f"Venv audio recording failed to start: {exc}"
                    self._notify_recording_error(msg)
        else:
            # Host-side capture via downloaded sounddevice binaries (no venv).
            try:
                ensure_native_binaries_on_path()
                import sounddevice as sd

                temp_path = self._take_new_temp_wav()
                wav = wave.open(temp_path, "wb")
                wav.setnchannels(self.channels)
                wav.setsampwidth(2)  # 16-bit
                wav.setframerate(self.fs)
                self.wav_file = wav
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
                        wav_obj = self.wav_file
                        # Status and the file are read together. Stop nulls
                        # wav_file under the same lock before close, so a
                        # chunk cannot pass this check and then write a
                        # closed file.
                        if self.state.status != "recording" or wav_obj is None:
                            return
                        try:
                            wav_obj.writeframes(pcm)
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

                stream = sd.RawInputStream(
                    samplerate=self.fs, channels=self.channels, dtype="int16", callback=callback
                )
                if not self._if_start_epoch_current(epoch, lambda: setattr(self, "stream", stream)):
                    best_effort(stream.close, "close stream")
                    self._close_host_wav()
                    self._handle_stale_start_exit(temp_path)
                    return

                self._notify_device_ready()
            except Exception as exc:
                self._close_host_wav()
                with self._lifecycle_lock:
                    stale = (self._epoch != epoch)
                self._handle_stale_start_exit(temp_path)
                if not stale:
                    self._notify_recording_error(
                        f"Audio recording failed to start. "
                        f"Please configure a Python venv or click 'Download Audio' in Settings → Python. Error: {exc}"
                    )

    def _execute_effect(self, effect: object) -> None:
        from plugin.framework.dev_build import is_dev_build

        testing = is_dev_build()
        if isinstance(effect, InitializeDeviceEffect):
            self._auto_stop_notified = False
            if testing:
                from plugin.chatbot.sidebar_test_hooks import try_stub_initialize

                if try_stub_initialize(self):
                    return

            with self._lifecycle_lock:
                self._epoch += 1
                epoch = self._epoch
                self._commit_pending = False
                self._discard_take = False

            self._start_job = run_in_background(
                self._start_capture,
                epoch,
                name="audio-rec-start",
                dedicated=True,
                daemon=True,
            )

        elif isinstance(effect, StartRecordingEffect):
            if self.stream is not None:
                try:
                    self.stream.start()
                except Exception as e:
                    self._apply_event(ErrorOccurredEvent(f"Audio recording failed to start stream: {e}"))

        elif isinstance(effect, StopRecordingEffect):
            if testing:
                from plugin.chatbot.sidebar_test_hooks import try_stub_stop

                if try_stub_stop(self):
                    return
            if self.stream is not None:
                self._teardown_capture()

            with self._lifecycle_lock:
                start_alive = self._start_job is not None and self._start_job.is_alive()
                proc = self._proc
                monitor = self._stdout_monitor
                handoff = self._stop_handoff
                auto_path = self._auto_stopped_path
                temp_path = self.temp_filename

                self._proc = None
                self._stdout_monitor = None
                self._stop_handoff = None
                self._auto_stopped_path = None

                # If the start job is alive and _stop_handoff is None, bump epoch,
                # kill_process_tree without waiting on UI thread; the start job reaps.
                if start_alive and handoff is None:
                    self._epoch += 1
                    if proc is not None:
                        self._background_rec_stop(kill_process_tree, proc.popen)
                    if not self._discard_take:
                        self._commit_pending = True
                        self.stop_deferred = True
                    self.temp_filename = None
                    return

                if proc is not None and temp_path and self.state.status != "error":
                    if proc.is_alive() and handoff is not None:
                        try:
                            signal_recording_stop(proc)
                        except Exception as exc:
                            log.warning("signal_recording_stop failed: %s", exc)
                    if not self._discard_take:
                        self._commit_pending = True
                        self.stop_deferred = True
                    self.temp_filename = None
                    self._stop_job = self._background_rec_stop(
                        self._finish_stop,
                        proc,
                        handoff,
                        monitor,
                        auto_path,
                        temp_path,
                    )
                    return
                else:
                    if proc is not None:
                        self._stop_job = self._background_rec_stop(
                            terminate_recording_process,
                            proc,
                            monitor=monitor,
                            handoff=handoff,
                        )

            if self.state.status == "error":
                # Failed start still deletes an empty file. A child error
                # after ready hits this same branch, and _cleanup_failed_start
                # Only delete empty WAV on error; non-empty files remain for transcription.
                if not _wav_file_has_bytes(self.temp_filename):
                    self._teardown_capture()
                    self._delete_wav()

        elif isinstance(effect, ReportErrorEffect):
            raise RuntimeError(effect.error_message)

    def _apply_event(self, event: AudioRecorderEvent) -> None:
        step = next_state(self.state, event)
        self.state = step.state
        for effect in step.effects:
            self._execute_effect(effect)

    def start_recording(self) -> None:
        self.stop_deferred = False
        self._apply_event(StartRequestedEvent())

    def stop_recording(self) -> str | None:
        self.stop_deferred = False
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
        with self._lifecycle_lock:
            self._discard_take = True
            self._commit_pending = False

        if self._proc is not None or self.stream is not None or self.state.status in ("initializing", "recording"):
            try:
                self._apply_event(StopRequestedEvent())
            except Exception:
                # A failed Stop must not leave the host WAV open. The
                # callback can still be inside writeframes, so stop the
                # stream first (that waits for the callback) and close after.
                self._teardown_capture()
        self._close_host_wav()
        self._delete_wav()

    def _delete_wav(self) -> None:
        if self.temp_filename:
            try:
                os.remove(self.temp_filename)
            except OSError as exc:
                log.debug("Failed to remove temp_filename: %s", exc)
            self.temp_filename = None
