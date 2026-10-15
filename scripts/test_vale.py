#!/usr/bin/env python3
"""Diagnostic script to test Vale style linter integration outside LibreOffice."""

import sys
import os
import time
from pathlib import Path

# Resolve project root dynamically (scripts/test_vale.py -> project root)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

print("Starting Vale style linter diagnostic test...")
print(f"Using Python interpreter: {sys.executable}")
print(f"Project root resolved to: {PROJECT_ROOT}")

# Step 1: Check if binary exists
try:
    print("\n--- Step 1: Locating Vale binary ---")
    venv_bin = Path(sys.executable).parent
    suffix = ".exe" if os.name == "nt" else ""
    vale_path = venv_bin / f"vale{suffix}"
    if not vale_path.exists():
        print(f"ERROR: Vale binary not found at '{vale_path}'.")
        print("Please run 'uv pip install vale' or 'pip install vale' in your virtual environment first.")
        sys.exit(1)
    print(f"Vale binary located successfully at: {vale_path}")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

# Step 2: Initialize Vale config and sync styles
user_config_dir = "/tmp/writeragent_vale_test"
print(f"\n--- Step 2: Initializing config directory at '{user_config_dir}' ---")
try:
    from plugin.scripting.venv.vale import run_vale_check
    
    # We run a check with sample text, which will trigger first-run setup (.vale.ini and vale sync)
    text = "We should utilize active voice to collaborate together, which is very unique. The button was clicked by the user at 12 pm noon because we are looking to achieve a synergistic reaction. Obviously, there are some weasel words here, but we are executing checks on this sentence. Do not make assumptions, as this is a simple opportunity. Hopefully it works now."
    print("Running initial check (this will download Microsoft, Google, and write-good style guides on first run)...")
    
    start = time.monotonic()
    res = run_vale_check(text, user_config_dir, "Microsoft,Google,write-good")
    print(f"Check completed in {time.monotonic() - start:.3f}s")
    
    print("\n--- Results ---")
    errors = res.get("errors", [])
    print(f"Found {len(errors)} style warnings:")
    for idx, err in enumerate(errors):
        print(f"  [{idx}] Rule: {err.get('rule_identifier')} | Type: {err.get('type')}")
        print(f"      Wrong text: '{err.get('wrong')}'")
        if err.get("correct"):
            print(f"      Suggested replacement: '{err.get('correct')}' (Alternatives: {err.get('suggestions')})")
        print(f"      Message: {err.get('reason')}")
        print(f"      Description: {err.get('full_comment')[:120]}...")
except Exception as e:
    print(f"ERROR executing check: {e}")
    sys.exit(1)
