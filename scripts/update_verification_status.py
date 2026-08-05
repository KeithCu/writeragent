#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Audit verification tests and update verification_status.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT_DIR / "verification_status.json"


def update_status() -> None:
    if not STATUS_FILE.exists():
        print(f"Error: {STATUS_FILE} not found")
        return

    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Simple audit count
    total_verified_functions = 0
    total_modules = 0

    for category, modules in data.items():
        if isinstance(modules, dict):
            for mod_name, mod_info in modules.items():
                if isinstance(mod_info, dict):
                    total_modules += 1
                    contracted = mod_info.get("functions_contracted", [])
                    total_verified_functions += len(contracted)

    print("Verification Audit Summary:")
    print(f"  Tracked Modules: {total_modules}")
    print(f"  Contracted Functions: {total_verified_functions}")

    # Re-save with clean formatting
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"✅ Updated {STATUS_FILE.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    update_status()
