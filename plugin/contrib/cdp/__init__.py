# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chrome DevTools Protocol helpers (adapted from Nous Research Hermes Agent, MIT).

CDP uses a client-only ``websockets`` asyncio subset (``connect``, ``WebSocketException``).
The full PyPI package is vendored then pruned at OXT build time by
``scripts/prune_vendored_websockets.py`` (legacy/sync/server code removed).
"""
