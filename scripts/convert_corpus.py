#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert spreadsheets in a directory to their `=PY()` equivalents.

Usage:
    python scripts/convert_corpus.py [path/to/dir]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Setup environment to prevent popup windows or background processes if possible
os.environ["WRITERAGENT_TESTING"] = "1"

try:
    import officehelper
    import uno
except ImportError:
    print("ERROR: LibreOffice PyUNO modules are not available in this Python environment.", file=sys.stderr)
    print("Please run this script using LibreOffice's Python interpreter.", file=sys.stderr)
    sys.exit(1)


def verify_py_addin(ctx) -> bool:
    """Confirm =PY() resolves and recalculates (WriterAgent extension must be deployed)."""
    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))
    if not doc:
        return False
    try:
        sheet = doc.getSheets().getByIndex(0)
        sheet.getCellByPosition(0, 0).setValue(1.0)
        sheet.getCellByPosition(0, 1).setValue(2.0)
        sheet.getCellByPosition(1, 0).setFormula('=PY("np.sum(data)";A1:A2)')
        doc.calculateAll()
        cell = sheet.getCellByPosition(1, 0)
        err = cell.getError()
        if err != 0:
            return False
        try:
            return abs(cell.getValue() - 3.0) < 1e-9
        except Exception:
            return False
    finally:
        try:
            doc.close(True)
        except Exception:
            pass


def convert_spreadsheet(ctx, file_path: Path) -> bool:
    print(f"\nProcessing: {file_path.name}")
    from plugin.framework.uno_context import get_desktop
    from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion

    desktop = get_desktop(ctx)
    file_url = uno.systemPathToFileUrl(str(file_path.resolve()))

    # Load spreadsheet hidden
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    try:
        doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden_prop,))
    except Exception as e:
        print(f"  Error loading file: {e}")
        return False

    if not doc:
        print("  Failed to load document.")
        return False

    try:
        sheets = doc.getSheets()
        num_sheets = sheets.getCount()
        print(f"  Found {num_sheets} sheets.")

        for i in range(num_sheets):
            sheet = sheets.getByIndex(i)
            sheet_name = sheet.getName()
            print(f"  Converting sheet '{sheet_name}'...")
            try:
                res = run_sheet_conversion(
                    doc,
                    sheet,
                    scope="sheet",
                    output_mode="in_place",
                    vectorize=True,
                    verify=True,
                )
                report = res.get("report", {})
                failed = res.get("failed_verifications", [])
                
                converted_count = len(report.get("converted", []))
                skipped_count = len(report.get("skipped", []))
                print(f"    - Converted: {converted_count} cells")
                print(f"    - Skipped: {skipped_count} cells")
                
                if failed:
                    print(f"    - WARNING: {len(failed)} verification mismatches:")
                    for fail in failed:
                        print(f"      * Cell {fail.get('address')}: expected {fail.get('expected')!r}, got {fail.get('actual')!r} ({fail.get('message')})")
                else:
                    print("    - Verification PASSED for all converted cells.")
            except Exception as e:
                print(f"    - Error converting sheet: {e}")

        # Save as _py equivalent
        suffix = "_py" + file_path.suffix
        out_name = file_path.stem + suffix
        out_path = file_path.parent / out_name
        out_url = uno.systemPathToFileUrl(str(out_path.resolve()))

        print(f"  Saving converted spreadsheet to: {out_path.name}")
        doc.storeToURL(out_url, ())
        return True

    except Exception as e:
        print(f"  Unexpected error during processing: {e}")
        return False
    finally:
        try:
            doc.close(True)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert spreadsheets to `=PY()` equivalents.")
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing spreadsheets to convert (default: current directory).",
    )
    args = parser.parse_args()

    dir_path = Path(args.directory).resolve()
    if not dir_path.is_dir():
        print(f"ERROR: {dir_path} is not a valid directory.", file=sys.stderr)
        return 1

    print(f"Scanning directory: {dir_path}")
    files = []
    # Non-recursive check for *.xlsx and *.ods
    for entry in os.scandir(dir_path):
        if entry.is_file():
            path = Path(entry.path)
            # Skip already converted files to avoid infinite loops
            if path.suffix.lower() in (".xlsx", ".ods") and not path.stem.endswith("_py"):
                files.append(path)

    if not files:
        print("No spreadsheet files (*.xlsx, *.ods) found in the specified directory.")
        return 0

    print(f"Found {len(files)} files to convert.")

    print("Bootstrapping LibreOffice...")
    try:
        ctx = officehelper.bootstrap()
    except Exception as e:
        print(f"ERROR: Failed to bootstrap LibreOffice: {e}", file=sys.stderr)
        return 1

    if not ctx:
        print("ERROR: officehelper.bootstrap() returned None.", file=sys.stderr)
        return 1

    # Full plugin.main.bootstrap() is not invoked here: it can segfault in a
    # headless officehelper session. The PY Calc add-in registers via extension
    # XCU independently; preflight below verifies it is available.
    print("Checking PY add-in registration...")
    if not verify_py_addin(ctx):
        print(
            "ERROR: =PY() add-in is not available in this LibreOffice session.\n"
            "Deploy WriterAgent (make deploy), restart LibreOffice, then re-run this script.",
            file=sys.stderr,
        )
        return 1
    print("PY add-in preflight passed.")

    success_count = 0
    for file_path in sorted(files):
        if convert_spreadsheet(ctx, file_path):
            success_count += 1

    print(f"\nDone. Successfully converted {success_count}/{len(files)} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
