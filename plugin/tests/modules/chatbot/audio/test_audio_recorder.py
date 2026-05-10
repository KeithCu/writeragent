import os
import wave
import pytest
import time
from unittest.mock import MagicMock, patch
from plugin.modules.chatbot.audio_recorder import AudioRecorder

def test_audio_recorder_creates_file_mocked():
    # Keep the original mocked test to ensure basic state logic works
    with patch.dict("sys.modules", {"sounddevice": MagicMock()}):
        recorder = AudioRecorder()
        temp_file = None
        try:
            recorder.start_recording()
            temp_file = recorder.temp_filename

            assert temp_file is not None
            assert os.path.exists(temp_file)
            assert recorder.state.status == 'recording'
            assert recorder.wav_file is not None

            returned_file = recorder.stop_recording()

            assert returned_file == temp_file
            assert recorder.state.status == 'idle'
            assert recorder.wav_file is None
            assert recorder.stream is None

            with wave.open(temp_file, 'rb') as wf:
                assert wf.getnchannels() == recorder.channels
                assert wf.getframerate() == recorder.fs
        finally:
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)

def test_audio_recorder_multiple_recordings_mocked():
    with patch.dict("sys.modules", {"sounddevice": MagicMock()}):
        recorder = AudioRecorder()
        files = []
        try:
            recorder.start_recording()
            files.append(recorder.temp_filename)
            recorder.stop_recording()

            recorder.start_recording()
            files.append(recorder.temp_filename)
            recorder.stop_recording()

            assert len(files) == 2
            assert files[0] != files[1]
            assert os.path.exists(files[0])
            assert os.path.exists(files[1])
        finally:
            for f in files:
                if os.path.exists(f):
                    os.remove(f)

def test_audio_recorder_real_hardware():
    """
    Integration test to verify real audio hardware interaction.
    If no audio hardware is found, it safely skips rather than failing the suite.
    """
    try:
        import sounddevice as sd
        # Query devices to see if there is any input device available.
        devices = sd.query_devices()
        input_device_available = any(d.get('max_input_channels', 0) > 0 for d in devices)
        if not input_device_available:
            pytest.skip("No audio input devices found.")
    except Exception as e:
        pytest.skip(f"sounddevice cannot be loaded or queried: {e}")

    recorder = AudioRecorder()
    temp_file = None
    try:
        try:
            recorder.start_recording()
        except RuntimeError as e:
            # e.g., "Error querying device -1" or generic backend failure
            pytest.skip(f"Audio hardware failed to initialize: {e}")

        temp_file = recorder.temp_filename
        assert temp_file is not None
        assert os.path.exists(temp_file)

        # Record for a short duration
        time.sleep(0.5)

        returned_file = recorder.stop_recording()
        assert returned_file == temp_file

        # Verify it wrote a valid header and potentially some frames
        with wave.open(temp_file, 'rb') as wf:
            assert wf.getnchannels() == recorder.channels
            assert wf.getframerate() == recorder.fs
    finally:
        # Stop recording if we crashed mid-flight
        if getattr(recorder, 'state', None) and recorder.state.status in ('recording', 'initializing'):
            try:
                recorder.stop_recording()
            except Exception:
                pass
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)
