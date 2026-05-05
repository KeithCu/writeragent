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
import os
import sys
import wave
import tempfile

from plugin.modules.chatbot.audio_recorder_state import AudioRecorderState, StartRequestedEvent, DeviceReadyEvent, StopRequestedEvent, ErrorOccurredEvent, InitializeDeviceEffect, StartRecordingEffect, StopRecordingEffect, ReportErrorEffect, next_state


class AudioRecorder:
    def __init__(self):
        self.fs = 16000  # Sample rate
        self.channels = 1
        self.stream = None
        self.wav_file = None
        self.temp_filename = None

        # Initialize pure state
        self.state = AudioRecorderState(status="idle")

    def _cleanup_failed_start(self):
        """Clean up resources if stream creation/start fails."""
        # Close and remove the temporary WAV file if we created one
        if self.wav_file is not None:
            try:
                self.wav_file.close()
            except (OSError, IOError) as e:
                import logging

                logging.getLogger(__name__).debug("Failed to close wav_file during cleanup: %s", e)
            self.wav_file = None
        if self.temp_filename:
            try:
                os.remove(self.temp_filename)
            except OSError as e:
                import logging

                logging.getLogger(__name__).debug("Failed to remove temp_filename during cleanup: %s", e)
            self.temp_filename = None
        # Best-effort stream cleanup
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug("Failed to stop stream during cleanup: %s", e)
            try:
                self.stream.close()
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug("Failed to close stream during cleanup: %s", e)
            self.stream = None

    def _execute_effect(self, effect):
        if isinstance(effect, InitializeDeviceEffect):
            try:
                import sounddevice as sd  # type: ignore[import-untyped]
            except OSError:
                self._apply_event(ErrorOccurredEvent("Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2"))
                return

            try:
                fd, self.temp_filename = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                self.wav_file = wave.open(self.temp_filename, "wb")
                self.wav_file.setnchannels(self.channels)
                self.wav_file.setsampwidth(2)  # 16-bit
                self.wav_file.setframerate(self.fs)

                def callback(indata, frames, time_info, status):
                    if status:
                        print(status, file=sys.stderr)
                    # Use state directly to decide if we should write
                    if self.state.status == "recording" and self.wav_file:
                        # sounddevice returns bytes if we pass dtype='int16' when opening as RawInputStream
                        self.wav_file.writeframes(indata)

                self.stream = sd.RawInputStream(samplerate=self.fs, channels=self.channels, dtype="int16", callback=callback)

                # Signal readiness
                self._apply_event(DeviceReadyEvent())

            except AssertionError:
                # Some PortAudio backends raise AssertionError (e.g. structVersion mismatch)
                self._apply_event(ErrorOccurredEvent("Audio recording is not available on this system (PortAudio backend error)."))
            except OSError:
                # Preserve the existing PortAudio missing-library hint
                self._apply_event(ErrorOccurredEvent("Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2"))
            except Exception as e:
                # Generic fallback for other backend errors
                self._apply_event(ErrorOccurredEvent(f"Audio recording failed to start: {e}"))

        elif isinstance(effect, StartRecordingEffect):
            try:
                if self.stream:
                    self.stream.start()
            except Exception as e:
                self._apply_event(ErrorOccurredEvent(f"Audio recording failed to start stream: {e}"))

        elif isinstance(effect, StopRecordingEffect):
            if self.stream:
                try:
                    self.stream.stop()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).debug("Failed to stop stream on StopRecordingEffect: %s", e)
                try:
                    self.stream.close()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).debug("Failed to close stream on StopRecordingEffect: %s", e)
                self.stream = None

            if self.wav_file:
                try:
                    self.wav_file.close()
                except (OSError, IOError) as e:
                    import logging

                    logging.getLogger(__name__).debug("Failed to close wav_file on StopRecordingEffect: %s", e)
                self.wav_file = None

            # If we error'd before creating a file, temp_filename could be empty/removed
            if self.state.status == "error":
                self._cleanup_failed_start()

        elif isinstance(effect, ReportErrorEffect):
            # Let the exception bubble up to the caller just like the old version
            raise RuntimeError(effect.error_message)

    def _apply_event(self, event):
        """Advances the state machine and executes effects synchronously."""
        step = next_state(self.state, event)
        self.state = step.state
        for effect in step.effects:
            self._execute_effect(effect)

    def start_recording(self):
        self._apply_event(StartRequestedEvent())

    def stop_recording(self):
        self._apply_event(StopRequestedEvent())

        # After stopping, return the temp filename (which may be None if error occurred)
        return self.temp_filename
