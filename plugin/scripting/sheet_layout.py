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

# Every layout key a spec or trusted-action packet can carry. Keep
# trusted_rpc._RESERVED_PAYLOAD_KEYS in sync: these keys must not be
# injectable through additional_data.
SHEET_LAYOUT_KEYS: tuple[str, ...] = ("headers", "header_row")


def extract_sheet_layout(source: Any) -> dict[str, Any]:
    """Extract the ``SHEET_LAYOUT_KEYS`` fields from a spec or packet dictionary."""
    if not isinstance(source, dict):
        return {}
    # Do not bool() or int() these fields on the host: bool("false") is True,
    # and int("abc") / int(None) raise before worker validation. Copy raw values.
    return {key: source[key] for key in SHEET_LAYOUT_KEYS if key in source}
