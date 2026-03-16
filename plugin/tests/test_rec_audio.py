import subprocess
import os
import sys

# Test basic os recording
if sys.platform == "win32":
    print("windows")
elif sys.platform == "darwin":
    print("mac")
else:
    print("linux")
