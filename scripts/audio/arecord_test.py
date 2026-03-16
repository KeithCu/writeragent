import subprocess

print("Testing arecord...")
try:
    p = subprocess.Popen(["arecord", "-f", "cd", "-t", "wav", "-d", "3", "test.wav"])
    p.wait()
    print("arecord success")
except Exception as e:
    print(f"arecord failed: {e}")

print("Testing rec (sox)...")
try:
    p = subprocess.Popen(["rec", "-d", "3", "test2.wav"])
    p.wait()
    print("rec success")
except Exception as e:
    print(f"rec failed: {e}")
