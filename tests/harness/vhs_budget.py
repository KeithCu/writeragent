# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared deep-Hypothesis budget flag for ``make vhs`` / ``make slowtests``.

``WRITERAGENT_VHS_EXTENSIVE`` is the preferred name. ``WRITERAGENT_SERIALIZATION_EXTENSIVE``
remains an alias so older Make/docs keep working.
"""

from __future__ import annotations

import os

_VHS_EXTENSIVE_ENV = "WRITERAGENT_VHS_EXTENSIVE"
_SERIALIZATION_EXTENSIVE_ENV = "WRITERAGENT_SERIALIZATION_EXTENSIVE"


def vhs_extensive() -> bool:
    """True when Make requests deep Hypothesis fuzzing (not default pytest)."""
    for key in (_VHS_EXTENSIVE_ENV, _SERIALIZATION_EXTENSIVE_ENV):
        if os.environ.get(key, "").lower() in ("1", "true", "yes"):
            return True
    return False


def vhs_max_examples(light: int, extensive: int) -> int:
    """Pick light (``make verify``) vs deep (``make vhs``) Hypothesis example counts."""
    return extensive if vhs_extensive() else light
