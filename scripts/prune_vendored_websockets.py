#!/usr/bin/env python3
# WriterAgent — prune vendored websockets for OXT (client-only CDP)
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove unused websockets subpackages before shipping in plugin/lib/.

WriterAgent CDP (browser_cdp_tool, browser_supervisor) is a **client-only**
asyncio user: ``websockets.connect``, ``WebSocketException``, ``ClientConnection``.
The full PyPI tree includes legacy, sync, and server APIs we never import.

Called from ``scripts/build_oxt.py`` after copying vendor/websockets into the
bundle. Do not run on the dev ``vendor/`` tree (``make vendor`` would overwrite
manual edits anyway).
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys

# Directories safe to drop for CDP client use.
_PRUNE_DIRS = (
    "legacy",
    "sync",
)

# Files safe to drop (paths relative to the websockets package root).
_PRUNE_FILES = (
    "asyncio/server.py",
    "asyncio/router.py",
    "server.py",
    "cli.py",
    "__main__.py",
    "auth.py",
    "http.py",
    "connection.py",
    "speedups.c",
    "speedups.pyi",
)

# Glob patterns for optional native speedups (platform-specific wheels).
_PRUNE_GLOBS = (
    "speedups.cpython-*.so",
    "speedups.cpython-*.pyd",
)


def prune_vendored_websockets(websockets_root: str) -> list[str]:
    """Delete CDP-unused paths under ``websockets_root``. Returns removed paths."""
    if not os.path.isdir(websockets_root):
        raise FileNotFoundError(f"websockets package not found: {websockets_root}")

    removed: list[str] = []

    for name in _PRUNE_DIRS:
        path = os.path.join(websockets_root, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(name + "/")

    for rel in _PRUNE_FILES:
        path = os.path.join(websockets_root, rel)
        if os.path.isfile(path):
            os.remove(path)
            removed.append(rel)

    for pattern in _PRUNE_GLOBS:
        for path in glob.glob(os.path.join(websockets_root, pattern)):
            if os.path.isfile(path):
                os.remove(path)
                removed.append(os.path.relpath(path, websockets_root))

    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune vendored websockets for OXT (CDP client-only).")
    parser.add_argument(
        "websockets_root",
        help="Path to plugin/lib/websockets (or a temp copy for tests)",
    )
    args = parser.parse_args(argv)

    removed = prune_vendored_websockets(os.path.abspath(args.websockets_root))
    if removed:
        print("Pruned websockets (%d paths):" % len(removed))
        for item in removed:
            print("  -", item)
    else:
        print("Nothing to prune (already minimal?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
