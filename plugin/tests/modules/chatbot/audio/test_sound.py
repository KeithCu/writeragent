import sys
import os
import pytest

try:
    import sounddevice
except OSError:
    pytest.skip("PortAudio library not found", allow_module_level=True)

sys.path.insert(0, os.path.abspath("contrib"))
try:
    import sounddevice as sd
    print("sounddevice version:", sd.__version__)
except OSError as e:
    print("Caught OSError as expected on Linux without libportaudio2 installed natively.", e)
