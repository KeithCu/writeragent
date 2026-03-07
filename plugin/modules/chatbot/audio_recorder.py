import sys
import wave
import tempfile

class AudioRecorder:
    def __init__(self):
        self.fs = 16000  # Sample rate
        self.channels = 1
        self.recording = False
        self.thread = None
        self.stream = None
        self.wav_file = None
        self.temp_filename = None

    def start_recording(self):
        try:
            import sounddevice as sd
        except OSError as e:
            raise RuntimeError("Audio recording requires PortAudio. On Linux, please run: sudo apt-get install libportaudio2") from e

        self.recording = True
        self.temp_filename = tempfile.mktemp(suffix=".wav")
        self.wav_file = wave.open(self.temp_filename, 'wb')
        self.wav_file.setnchannels(self.channels)
        self.wav_file.setsampwidth(2) # 16-bit
        self.wav_file.setframerate(self.fs)

        def callback(indata, frames, time_info, status):
            if status:
                print(status, file=sys.stderr)
            if self.recording:
                # indata is numpy array, but we don't have numpy.
                # sounddevice returns bytes if we pass dtype='int16' when opening as RawInputStream
                self.wav_file.writeframes(indata)

        self.stream = sd.RawInputStream(samplerate=self.fs, channels=self.channels, dtype='int16', callback=callback)
        self.stream.start()

    def stop_recording(self):
        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if self.wav_file:
            self.wav_file.close()
            self.wav_file = None

        return self.temp_filename

_recorder = AudioRecorder()

def start_recording():
    _recorder.start_recording()

def stop_recording():
    return _recorder.stop_recording()
