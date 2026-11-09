#!/usr/bin/env python3
"""Diagnostic script to test Harper Rust linter integration outside LibreOffice."""

import sys
import os
import time

# Resolve project root dynamically
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

print("Starting Harper linter diagnostic test...")
print(f"Using Python interpreter: {sys.executable}")
print(f"Project root resolved to: {PROJECT_ROOT}")

# Step 1: Initialize config directory and trigger check
user_config_dir = "/tmp/writeragent_harper_test"
print(f"\n--- Step 1: Initializing config directory at '{user_config_dir}' ---")

try:
    from plugin.writer.locale.harper import harper_try_lint, runtime

    # Sample text with spelling error (there), capitalization, and spaces
    text = "this is a test sentence. there is some spelling errors and a double  space."
    print("Running check (this will trigger platform-specific binary auto-download on first run)...")

    runtime.ensure_body(user_config_dir)
    start = time.monotonic()
    res = harper_try_lint(text, user_config_dir)
    print(f"Check completed in {time.monotonic() - start:.3f}s")

    if res is None:
        print("ERROR: harper_try_lint returned None (harper-ls not ready or lint failed)")
        sys.exit(1)

    print("\n--- Results ---")
    errors = res.get("errors", [])
    print(f"Found {len(errors)} warnings/errors:")
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
