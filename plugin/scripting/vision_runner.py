# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted vision execution for Run Python Script (Writer graphic export + venv RPC)."""

from __future__ import annotations

import base64
from typing import Any

from plugin.framework.client.vision_client import run_vision
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting.vision import HELPER_NAMES
from plugin.writer.images.image_tools import get_selected_image_base64


def get_selected_image_bytes(ctx: Any, doc: Any) -> bytes:
    """Export the currently selected embedded graphic as raw PNG bytes."""
    b64 = get_selected_image_base64(doc, ctx)
    if not b64:
        raise ToolExecutionError(
            _("Select an embedded image, then Run again."),
            code="NO_IMAGE_SELECTED",
        )
    return base64.b64decode(b64)


def run_trusted_vision(
    ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export selected graphic bytes and run a trusted vision helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")

    png_bytes = get_selected_image_bytes(ctx, doc)
    spec: dict[str, Any] = {"helper": name, "params": dict(params) if isinstance(params, dict) else {}}
    context = {"source": "selection"}
    return run_vision(ctx, spec, png_bytes, context=context)
