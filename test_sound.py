import sys
import os
sys.path.insert(0, os.path.abspath("plugin/vendor"))
try:
    import sounddevice as sd
    print("sounddevice version:", sd.__version__)
except OSError as e:
    print("Caught OSError as expected on Linux without libportaudio2 installed natively.", e)
