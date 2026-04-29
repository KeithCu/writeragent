# Enabling Basic Numpy in LibreOffice

Getting a C-compiled library like `numpy` to run reliably inside a LibreOffice extension is challenging, primarily because LibreOffice ships with its own embedded Python interpreter. 

This document outlines what it takes to get `numpy` functioning properly in your extension and evaluates the potential approaches.

## The Core Challenge: ABI Mismatches
`numpy` is not a pure Python library; it contains compiled C/C++ extensions. These compiled libraries must be built against the exact version and ABI (Application Binary Interface) of the Python interpreter that runs them.

- **The Problem**: If a user runs `pip install numpy` using their system Python (e.g., Python 3.12) and your extension loads that `numpy` bundle into LibreOffice's embedded Python (e.g., Python 3.8 or 3.9), the entire LibreOffice instance will fatally crash because the C-extensions are binary-incompatible.
- **The Requirement**: To run `numpy`, it **must** be downloaded or compiled using the exact `python` executable that LibreOffice is using.

---

## Strategy 1: The LibrePythonista Approach (Pip Bootstrapping)
Instead of trying to ship `numpy` inside the `.oxt` extension file, the extension ships with `pip` and installs `numpy` directly into LibreOffice's environment at runtime.

### What it takes:
1. Bundle a script like `get-pip.py` or a `pip` `.whl` inside your `.oxt`.
2. On extension startup, resolve the physical path to LibreOffice's python (using `sys.executable` or inferring it from `uno.__file__`).
3. Determine a safe user-writable path (handling quirks for Windows, macOS, Flatpak sandbox boundaries).
4. Run a background process: `[sys.executable, 'get-pip.py', '--target', safe_path]`.
5. Run a second background process: `[sys.executable, '-m', 'pip', 'install', 'numpy', '--target', safe_path]`.
6. At the top of your extension script, `sys.path.append(safe_path)` and then `import numpy`.

*(Note: LibrePythonista contains over 2,000 lines of code specifically dedicated to handling the weird edge cases of OS and Flatpak paths to make this reliable).*

---

## Strategy 2: The Managed Venv Approach (Recommended)
This approach simplifies the LibrePythonista method. Instead of manually overriding target directories, you just have the extension create a standard virtual environment using the LibreOffice Python. By using a standard `.venv`, `pip` is included automatically.

### What it takes:
1. When your extension is initialized, check if a target directory (e.g., `~/.writeragent_venv`) exists.
2. If it does not exist, spawn a subprocess to create it using the embedded interpreter:
   ```python
   import subprocess
   import sys
   
   # Creates a venv using the exact python executable running this script
   subprocess.run([sys.executable, "-m", "venv", "/path/to/user/home/.writeragent_venv"])
   ```
3. Use the newly created venv's python to install `numpy`:
   ```python
   venv_python = "/path/to/user/home/.writeragent_venv/bin/python" # (or Scripts/python.exe on Windows)
   subprocess.run([venv_python, "-m", "pip", "install", "numpy"])
   ```
4. On all subsequent runs, before you import `numpy`, dynamically inject the venv into the path:
   ```python
   import sys
   # Append the venv site-packages so numpy can be found
   sys.path.insert(0, "/path/to/user/home/.writeragent_venv/lib/python3.x/site-packages")
   import numpy
   ```

**Pros**: Highly reliable, automatically provides `pip`, guarantees ABI compatibility, prevents polluting LibreOffice's global state.
**Cons**: Requires the user to have an internet connection the very first time they trigger the feature, so `numpy` can be fetched.

---

## Strategy 3: Pointing to an Existing "User-Provided" Venv
Rather than creating or bootstrapping a Python environment internally, you might add a setting that lets the user point to an existing `.venv` directory they already created on their system.

If you choose this route, there are two fundamentally different ways you can execute their `numpy` installation:

### A: The Dangerous Way (In-Process `sys.path` Injection)
You configure your extension to read the user's provided path and append it to LibreOffice's internal Python path:
```python
import sys
# The user types this path into a LibreOffice settings dialog
user_venv_path = get_user_setting("custom_venv_path")
sys.path.insert(0, f"{user_venv_path}/lib/python3.x/site-packages")

import numpy # Will attempt to load from the user's venv
```
- **The Catch**: This is notoriously fragile. If the user created their `.venv` using their system's Python 3.12, but LibreOffice embeds Python 3.8, `numpy` will immediately crash with a fatal ABI/DLL error. This approach *only* works if the user went out of their way to purposefully construct their `.venv` using the exact minor version (and architecture) of the Python interpreter embedded in LibreOffice.

### B: The Safe Way (Out-of-Process Execution / RPC)
Instead of importing `numpy` inside the LibreOffice Python instance, you never modify `sys.path`. Instead, your extension acts as a thin UI that shells out to the `python` executable located *inside* the user's venv.
1. The user interacts with the UI in LibreOffice.
2. The extension writes target data to a temporary JSON or CSV file.
3. The extension triggers the user's external Python process using `subprocess.Popen("/their/custom/venv/bin/python worker.py")`.
4. That background process loads `numpy`, applies operations, and writes the results back.
5. The LibreOffice extension reads the results back into the spreadsheet/document.

**Pros**: Completely sidesteps ABI issues and embedded interpreter limits. `Numpy` will never crash LibreOffice because the two Python interpreters never mix memory. 
**Cons**: Slower execution due to file/socket I/O overhead. Requires you to handle subprocess lifecycles reliably.
