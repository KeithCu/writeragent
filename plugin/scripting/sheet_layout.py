# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy ``headers`` and ``header_row`` off a trusted-action spec or packet.

Leaf module. The host client and the venv child both import this. It must not
import ``trusted_rpc`` or ``venv_worker``: those load the host worker into the child.
"""

from __future__ import annotations

from typing import Any


def extract_sheet_layout(source: Any) -> dict[str, Any]:
    """Extract ``headers`` and ``header_row`` from a spec or packet dictionary."""
    layout: dict[str, Any] = {}
    if isinstance(source, dict):
        # Do not bool() or int() these fields on the host: bool("false") is True,
        # and int("abc") / int(None) raise before worker validation. Copy raw values.
        if "headers" in source:
            layout["headers"] = source["headers"]
        if "header_row" in source:
            layout["header_row"] = source["header_row"]
    return layout
