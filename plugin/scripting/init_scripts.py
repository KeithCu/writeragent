# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-workbook Calc initialization scripts stored in UserDefinedProperties."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from plugin.doc.document_helpers import get_document_property, is_calc, set_document_property
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.scripting.session_manager import calc_init_session_id, calc_workbook_base_session_id

log = logging.getLogger(__name__)

CALC_INIT_SCRIPT_UDPROP = "WriterAgentCalcInitScript"
_MAX_INIT_SCRIPT_BYTES = 900_000


def get_calc_init_script(doc: Any, *, default: str = "") -> str:
    raw = get_document_property(doc, CALC_INIT_SCRIPT_UDPROP, default=None)
    if raw is None:
        return default
    return str(raw)


def set_calc_init_script(doc: Any, code: str) -> str | None:
    """Persist init script on *doc*. Returns an error message when too large."""
    payload = code if code is not None else ""
    if len(payload.encode("utf-8")) > _MAX_INIT_SCRIPT_BYTES:
        return _("Initialization script is too large to store in the document ({0} bytes).").format(
            len(payload.encode("utf-8"))
        )
    set_document_property(doc, CALC_INIT_SCRIPT_UDPROP, payload)
    return None


def get_calc_document_from_ctx(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("init_scripts: could not resolve active document", exc_info=True)
        return None
    if doc is None or not is_calc(doc):
        return None
    return doc


def init_script_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def build_python_eval_init_kwargs(
    doc: Any,
    *,
    session_id: str | None,
) -> dict[str, Any]:
    """Worker kwargs for init execution before a ``=PYTHON()`` cell runs."""
    init_code = (get_calc_init_script(doc) or "").strip()
    if not init_code:
        return {}
    return {
        "init_script": init_code,
        "init_session_id": calc_init_session_id(doc),
        "init_script_hash": init_script_hash(init_code),
        "cell_session_id": session_id,
    }
