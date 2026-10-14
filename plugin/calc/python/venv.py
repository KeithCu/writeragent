# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LLM tool: run Python in the user-configured venv for Writer, Calc, and Draw (see plugin/scripting/venv_worker.py)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcPythonBase
from plugin.calc.calc_addin_data import resolve_python_data_on_main_thread
from plugin.scripting.import_policy import format_matplotlib_plot_hint
from plugin.scripting.payload_codec import find_image_payloads, is_image_payload, write_image_payload_to_temp
from plugin.scripting.venv_worker import run_code_in_user_venv

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_ALL_VENV_DOCS = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

_RESULT_NOTE = "Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar)."
_CODE_PROP = {
    "type": "string",
    "description": f"Python / Numpy source. {_RESULT_NOTE}",
}
_DATA_RANGE_DESCRIPTION = (
    "Optional A1 range(s) injected as CalcRange: one address → `data` is that range and `ranges == [data]`; "
    "two or more → `data` is the same list as `ranges`. Pass one address string, a comma/semicolon-separated string "
    "(e.g. 'A1:A10, C1:C10'), or an array of address strings."
)
_DATA_RANGE_SCHEMA = {
    "description": _DATA_RANGE_DESCRIPTION,
    # Single type: Gemini function calling rejects oneOf. Execute still
    # accepts a list of addresses if a model sends one.
    "type": "string",
}

_IMPORT_NOTE_MARKER = "Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar). "


def _with_import_note(text: str) -> str:
    """Insert the sandbox import policy when the tool schema is built.

    Read the global here, after ensuring init has run.
    PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE starts empty and is filled on first
    prompt assembly. Concatenating that name at import (tool discovery)
    captures the empty string, and a later assignment does not rewrite it.
    """
    from plugin.framework import prompts

    prompts._ensure_venv_import_policy_strings()
    note = prompts.PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    if note and _IMPORT_NOTE_MARKER in text:
        return text.replace(_IMPORT_NOTE_MARKER, _IMPORT_NOTE_MARKER + note, 1)
    return text


def _venv_schema_and_description(doc_type: str | None) -> tuple[str, dict[str, Any]]:
    """Build the description and JSON parameter schema for the given document type."""
    props: dict[str, Any] = {"code": dict(_CODE_PROP)}
    if doc_type == "calc":
        props["data_range"] = dict(_DATA_RANGE_SCHEMA)
        props["data"] = {
            "type": "array",
            "items": {"type": "array", "items": {}},
            "description": "Optional 2D array of cell values as `data` (use data_range for bulk data; the host resolves addresses without putting values in the LLM context).",
        }
        base_desc = (
            f"Run Python code. {_RESULT_NOTE} "
            "Optional data_range (one A1 address, comma-separated addresses, or an array) injects "
            "`data` / `ranges` (one address → `data` is that CalcRange; several → `data` is the `ranges` list). "
            "The host reads ranges on the main thread and sends shaped data over the efficient IPC path. "
            "For anything beyond tiny grids, use data_range (address) rather than passing values in the data parameter."
        )
    elif doc_type in ("draw", "impress"):
        base_desc = (
            f"Run Python code in the configured venv. {_RESULT_NOTE} "
            "Use document tools to read or change the slide/page; this tool does not inject spreadsheet `data`."
        )
    elif doc_type is None:
        props["data_range"] = {**_DATA_RANGE_SCHEMA, "description": "(Calc only) " + _DATA_RANGE_DESCRIPTION}
        props["data"] = {
            "type": "array",
            "items": {"type": "array", "items": {}},
            "description": "(Calc only) Optional 2D array of cell values as `data` (prefer data_range for bulk data).",
        }
        base_desc = (
            f"Run Python code in the configured venv. {_RESULT_NOTE} "
            "In Calc, optional data_range injects `data` / `ranges` from one or more A1 addresses; "
            "in Writer or Draw/Impress use document tools to read or change content (no spreadsheet `data` injection)."
        )
    else:
        base_desc = (
            f"Run Python code in the configured venv. {_RESULT_NOTE} "
            "Use document tools to read or change the file; this tool does not inject spreadsheet `data`."
        )

    hint = format_matplotlib_plot_hint(doc_type=doc_type)
    text = f"{base_desc} {hint}" if hint else base_desc
    desc = _with_import_note(text)
    params = {"type": "object", "properties": props, "required": ["code"]}
    return desc, params


# Bugfix: What was wrong: _strip_image_payloads dropped image payloads completely, collapsed lists, and dropped dictionary keys.
# How it happened: It filtered out None values resulting from image payloads, which altered list lengths and deleted dictionary keys or omitted the result key entirely.
# Why this change fixes it: It replaces any image payload with "<image>" while preserving collection lengths, sequence ordering, and dictionary keys.
_IMAGE_PLACEHOLDER = "<image>"


def _strip_image_payloads(obj: Any) -> Any:
    """Return result with image payloads replaced by '<image>', preserving structure."""
    if is_image_payload(obj):
        return _IMAGE_PLACEHOLDER
    if isinstance(obj, (list, tuple)):
        return [_strip_image_payloads(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _strip_image_payloads(v) for k, v in obj.items()}
    return obj


class RunVenvPythonScript(ToolCalcPythonBase):
    """Registered once; visible in Writer/Calc/Draw specialized ``domain=python`` via ``specialized_cross_cutting``."""

    name: str | None = "run_venv_python_script"
    specialized_cross_cutting: ClassVar[bool] = True
    description: str = (
        "Run Python code in the configured venv. Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"code": dict(_CODE_PROP)},
        "required": ["code"],
    }
    uno_services: list[str] | None = list(_ALL_VENV_DOCS)
    long_running: bool = True

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        return _venv_schema_and_description(doc_type)[1]

    def get_description(self, doc_type: str | None = None) -> str:
        return _venv_schema_and_description(doc_type)[0]

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        code = str(kwargs.get("code", ""))
        if kwargs.get("timeout_sec") is not None:
            log.debug("run_venv_python_script: ignoring timeout_sec (user setting controls wall clock)")

        py_data = None
        if ctx.doc_type == "calc":
            data_range = kwargs.get("data_range")
            data = kwargs.get("data")
            py_data, err = resolve_python_data_on_main_thread(ctx, data_range=data_range, data=data)
            if err:
                return {"status": "error", "message": err}
        else:
            if kwargs.get("data_range") is not None or kwargs.get("data") is not None:
                log.debug("run_venv_python_script: ignoring data/data_range on doc_type=%s", ctx.doc_type)

        # Pin ctx.doc for this call only. document_for_script_session
        # resolves a doc: id before the desktop's current component. The
        # pin is not the worker session id, so Isolated mode still gets a
        # fresh namespace. With no session id, host tool RPC uses
        # get_active_document and wa.draw / wa.shape bind to the front window.
        from plugin.scripting.session_manager import pin_script_document, release_script_document

        script_session_id = pin_script_document(ctx.doc)

        stop_checker = getattr(ctx, "stop_checker", None)
        cancellation_scope = getattr(ctx, "send_cancellation", None)

        try:
            res = run_code_in_user_venv(
                ctx.ctx,
                code,
                data=py_data,
                python_tool_domain=ctx.python_tool_domain,
                script_session_id=script_session_id,
                stop_checker=stop_checker,
                cancellation_scope=cancellation_scope,
            )
        finally:
            release_script_document(script_session_id)

        result = res.get("result")
        if res.get("status") == "ok":
            images = find_image_payloads(result)
            if images:
                # Bugfix: What was wrong: a write failure on a later image payload abandoned
                # earlier temp files and leaked them on disk.
                # How it happened: the list comprehension aborted on exception, but
                # write_image_payload_to_temp only unlinks a file whose own write failed,
                # leaving earlier successes uncollected before out exists.
                # Why this change fixes it: we append each successful path in a loop and,
                # on any exception, unlink all paths already written before re-raising.
                # Paths that later sit on out stay the caller's.
                img_paths: list[str] = []
                try:
                    for img in images:
                        img_paths.append(write_image_payload_to_temp(img))
                except Exception:
                    for p in img_paths:
                        try:
                            os.unlink(p)
                        except OSError:
                            log.debug("run_venv_python_script: temp image cleanup failed for %s", p, exc_info=True)
                    raise
                out: dict[str, Any] = {"status": "ok", "message": f"{len(images)} plot(s) generated"}
                stdout_val = res.get("stdout")
                if stdout_val:
                    out["stdout"] = stdout_val
                stripped_result = _strip_image_payloads(result)
                if stripped_result is not None:
                    out["result"] = stripped_result

                out["image_paths"] = img_paths
                if len(img_paths) == 1:
                    out["image_path"] = img_paths[0]
                else:
                    out["image_path"] = img_paths

                if ctx.doc_type == "calc":
                    from plugin.calc.python.image_egress import insert_image_result_on_sheet
                    from plugin.framework.queue_executor import execute_on_main_thread

                    # Pass ctx.doc, and report inserted only after the
                    # main-thread call returns. Omitting doc= makes egress
                    # use getCurrentComponent (the front window), and a
                    # silent return was treated as success.
                    inserted = 0
                    failure: Exception | None = None
                    for i, (img, path) in enumerate(zip(images, img_paths)):
                        try:
                            execute_on_main_thread(
                                insert_image_result_on_sheet,
                                ctx.ctx,
                                img,
                                doc=ctx.doc,
                                image_index=i,
                                image_path=path,
                                image_count=len(images),
                            )
                        except Exception as exc:
                            log.warning("run_venv_python_script: plot insert failed: %s", exc)
                            failure = exc
                            break
                        inserted += 1
                    if failure is not None and inserted == 0:
                        out["status"] = "error"
                        out["image_inserted"] = False
                        out["plot_error"] = str(failure)
                        out["message"] = f"Plot was not inserted: {failure}"
                    elif failure is not None:
                        out["image_inserted"] = True
                        out["plot_error"] = str(failure)
                        out["message"] = f"{inserted} of {len(images)} plot(s) inserted on active sheet"
                    else:
                        out["message"] = f"{len(images)} plot(s) inserted on active sheet"
                        out["image_inserted"] = True
                        # After a successful Calc embed, the bitmap is embedded on the shape.
                        # Remove the temp file(s) and omit image_path/image_paths.
                        for p in img_paths:
                            try:
                                os.unlink(p)
                            except OSError:
                                pass
                        out.pop("image_path", None)
                        out.pop("image_paths", None)
                return out

        return res
