#!/usr/bin/env python3
"""Diagnostic script to test LanguageTool integration outside LibreOffice."""

import sys
import os
import time

# Resolve project root dynamically (scripts/test_languagetool.py -> project root)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

print("Starting LanguageTool diagnostic test...")
print(f"Using Python interpreter: {sys.executable}")
print(f"Project root resolved to: {PROJECT_ROOT}")

# Step 1: Check direct imports
try:
    print("\n--- Step 1: Importing language-tool-python ---")
    start = time.monotonic()
    import language_tool_python
    print(f"Import successful in {time.monotonic() - start:.3f}s")
except ImportError:
    print("ERROR: language-tool-python is not installed in this environment.")
    sys.exit(1)

# Step 2: Initialize LanguageTool directly
try:
    print("\n--- Step 2: Initializing LanguageTool client ---")
    print("This may download the LanguageTool server JARs (~200MB) on the first run and boot the JVM, which can take some time...")
    start = time.monotonic()
    # Use en-US
    tool = language_tool_python.LanguageTool("en-US")
    print(f"Client initialized successfully in {time.monotonic() - start:.3f}s")
except Exception as e:
    print(f"ERROR initializing client: {e}")
    sys.exit(1)

# Step 3: Run a test check directly
try:
    print("\n--- Step 3: Performing grammar check ---")
    text = "A sentence with a error."
    start = time.monotonic()
    matches = tool.check(text)
    print(f"Check completed in {time.monotonic() - start:.3f}s")
    print(f"Found {len(matches)} issues:")
    for idx, m in enumerate(matches):
        print(f"  [{idx}] Rule: {getattr(m, 'rule_id', 'unknown')} | Message: {m.message}")
        print(f"      Text context: ...{m.context}...")
        print(f"      Replacements: {m.replacements[:3]}")
except Exception as e:

    print(f"ERROR checking text: {e}")


# Step 4: Run via WriterAgent's venv helper
try:
    print("\n--- Step 4: Calling WriterAgent's languagetool venv helper ---")
    from plugin.scripting.venv.languagetool import run_languagetool_check
    
    start = time.monotonic()
    res = run_languagetool_check("This has an error.", "en-US")
    print(f"Helper returned in {time.monotonic() - start:.3f}s")
    print(f"Result: {res}")
except Exception as e:
    print(f"ERROR calling helper: {e}")
