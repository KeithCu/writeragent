# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-workbook Calc initialization scripts stored in UserDefinedProperties."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from plugin.doc.document_helpers import is_calc
from plugin.framework.uno_context import get_desktop
from plugin.scripting.session_manager import calc_init_session_id

log = logging.getLogger(__name__)

from plugin.scripting.document_scripts import get_document_scripts, set_document_scripts

def get_calc_init_script(doc: Any, *, default: str = "") -> str:
    scripts = get_document_scripts(doc)
    return scripts.get("INIT") or scripts.get("Init") or default


def set_calc_init_script(doc: Any, code: str) -> str | None:
    """Persist init script on *doc* under the name 'INIT' or 'Init' in document scripts."""
    scripts = dict(get_document_scripts(doc))
    if code:
        if "Init" in scripts:
            scripts["Init"] = code
        else:
            scripts["INIT"] = code
    else:
        scripts.pop("INIT", None)
        scripts.pop("Init", None)
    return set_document_scripts(doc, scripts)


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


def build_python_eval_init_kwargs(doc: Any) -> dict[str, Any]:
    """Kwargs for ``run_code_in_user_venv`` init execution (pass with separate ``session_id=``)."""
    init_code = (get_calc_init_script(doc) or "").strip()
    if not init_code:
        return {}
    return {
        "init_script": init_code,
        "init_session_id": calc_init_session_id(doc),
        "init_script_hash": init_script_hash(init_code),
    }
