import os
import wave
from unittest.mock import MagicMock, patch
from plugin.modules.chatbot.audio_recorder import AudioRecorder

def test_audio_recorder_creates_file():
    # Mock sounddevice to avoid dependency issues
    with patch.dict("sys.modules", {"sounddevice": MagicMock()}):
        recorder = AudioRecorder()

        # Ensure we cleanup after test
        temp_file = None
        try:
            recorder.start_recording()
            temp_file = recorder.temp_filename

            assert temp_file is not None
            assert os.path.exists(temp_file)
            assert recorder.recording is True
            assert recorder.wav_file is not None

            returned_file = recorder.stop_recording()

            assert returned_file == temp_file
            assert recorder.recording is False
            assert recorder.wav_file is None
            assert recorder.stream is None

            # Check if it's a valid wav file (at least header is written)
            with wave.open(temp_file, 'rb') as wf:
                assert wf.getnchannels() == recorder.channels
                assert wf.getframerate() == recorder.fs

        finally:
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)

def test_audio_recorder_multiple_recordings():
    with patch.dict("sys.modules", {"sounddevice": MagicMock()}):
        recorder = AudioRecorder()

        files = []
        try:
            # First recording
            recorder.start_recording()
            files.append(recorder.temp_filename)
            recorder.stop_recording()

            # Second recording
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
