# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clone Impress/Draw shapes between pages (used by PPTX import).

Pictures are reimported through ``shape_clone.reimport_graphic``.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.draw.shape_clone import clone_shape
from plugin.framework.errors import is_real_disposal

log = logging.getLogger(__name__)


def copy_shapes_to_page(source_page: Any, target_doc: Any, target_page: Any, uno_ctx: Any | None = None) -> int:
    """Copy all shapes from *source_page* to *target_page*; return count copied."""
    copied = 0
    for i in range(source_page.getCount()):
        src = source_page.getByIndex(i)
        try:
            result = clone_shape(target_doc, target_page, src, uno_ctx=uno_ctx)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.warning("clone_shape failed: %s", exc)
            continue
        if result.graphic_shapes > result.graphic_reimports:
            log.warning("PPTX picture reimport failed; left Graphic unset rather than alias the hidden source pool")
        copied += 1
    return copied


def clear_page_shapes(page: Any) -> None:
    """Remove all shapes from a draw page (for re-export)."""
    while page.getCount() > 0:
        page.remove(page.getByIndex(page.getCount() - 1))
