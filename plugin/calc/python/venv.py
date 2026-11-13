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
from plugin.calc.calc_addin_data import resolve_python_data
from plugin.framework.constants import ALL_DOCUMENT_SERVICES
from plugin.framework.queue_executor import main
from plugin.scripting.import_policy import format_matplotlib_plot_hint
from plugin.scripting.payload_codec import find_image_payloads, strip_image_payloads, write_image_payload_to_temp
from plugin.scripting.venv_worker import run_code_in_user_venv

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

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


def _data_value_schema(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "array", "items": {}}, "description": description}


_IMPORT_NOTE_MARKER = _RESULT_NOTE + " "


def _with_import_note(text: str) -> str:
    """Insert the sandbox import policy when the tool schema is built."""
    from plugin.scripting import import_policy

    note = import_policy.python_venv_auto_imports_note()
    if _IMPORT_NOTE_MARKER in text:
        return text.replace(_IMPORT_NOTE_MARKER, _IMPORT_NOTE_MARKER + note, 1)
    return text


def _venv_schema_and_description(doc_type: str | None) -> tuple[str, dict[str, Any]]:
    """Build the description and JSON parameter schema for the given document type."""
    props: dict[str, Any] = {"code": dict(_CODE_PROP)}
    if doc_type == "calc":
        props["data_range"] = dict(_DATA_RANGE_SCHEMA)
        props["data"] = _data_value_schema("Optional 2D array of cell values as `data` (use data_range for bulk data; the host resolves addresses without putting values in the LLM context).")
        base_desc = (
            f"Run Python code. {_RESULT_NOTE} "
            "Optional data_range (one A1 address, comma-separated addresses, or an array) injects "
            "`data` / `ranges` (one address → `data` is that CalcRange; several → `data` is the `ranges` list). "
            "The host reads ranges on the main thread and sends shaped data over the efficient IPC path. "
            "For anything beyond tiny grids, use data_range (address) rather than passing values in the data parameter."
        )
    elif doc_type is None:
        props["data_range"] = {**_DATA_RANGE_SCHEMA, "description": "(Calc only) " + _DATA_RANGE_DESCRIPTION}
        props["data"] = _data_value_schema("(Calc only) Optional 2D array of cell values as `data` (prefer data_range for bulk data).")
        base_desc = (
            f"Run Python code in the configured venv. {_RESULT_NOTE} "
            "In Calc, optional data_range injects `data` / `ranges` from one or more A1 addresses; "
            "in Writer or Draw/Impress use document tools to read or change content (no spreadsheet `data` injection)."
        )
    else:
        where = "slide/page" if doc_type in ("draw", "impress") else "file"
        base_desc = (
            f"Run Python code in the configured venv. {_RESULT_NOTE} "
            f"Use document tools to read or change the {where}; this tool does not inject spreadsheet `data`."
        )

    hint = format_matplotlib_plot_hint(doc_type=doc_type)
    text = f"{base_desc} {hint}" if hint else base_desc
    desc = _with_import_note(text)
    params = {"type": "object", "properties": props, "required": ["code"]}
    return desc, params


def _unlink_quiet(paths: list[str]) -> None:
    """Best-effort delete. Paths not passed here stay the caller's."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            log.debug("run_venv_python_script: temp image cleanup failed for %s", path, exc_info=True)


def _report_image_paths(out: dict[str, Any], paths: list[str]) -> None:
    """Hand temp image files the user still needs back as image_paths / image_path (one path or the list)."""
    if paths:
        out["image_paths"] = paths
        out["image_path"] = paths[0] if len(paths) == 1 else paths


def insert_plots_and_report(ctx: ToolContext, images: list[dict[str, Any]], res: dict[str, Any]) -> dict[str, Any]:
    """Build the tool result for a venv run whose result holds plots.

    Every plot is written to a temp file. Outside Calc the paths are returned for
    the user. In Calc each plot is inserted on ctx.doc on the main thread; a failed
    slot does not stop later plots. Temp files of inserted plots are deleted, the
    failed ones are returned, and the result is an error only when no plot was inserted.
    """
    img_paths: list[str] = []
    try:
        for img in images:
            img_paths.append(write_image_payload_to_temp(img))
    except Exception:
        # Delete the temps written so far; the caller never sees these paths.
        _unlink_quiet(img_paths)
        raise
    out: dict[str, Any] = {"status": "ok", "message": f"{len(images)} plot(s) generated"}
    stdout_val = res.get("stdout")
    if stdout_val:
        out["stdout"] = stdout_val
    stripped_result = strip_image_payloads(res.get("result"))
    if stripped_result is not None:
        out["result"] = stripped_result
    if ctx.doc_type != "calc":
        _report_image_paths(out, img_paths)
        return out

    from plugin.calc.python.image_egress import insert_image_result_on_sheet

    inserted_paths: list[str] = []
    failed_paths: list[str] = []
    failures: list[Exception] = []
    for i, (img, path) in enumerate(zip(images, img_paths, strict=True)):
        try:
            # Pass ctx.doc: without doc= egress uses getCurrentComponent (the front
            # window). Report inserted only after the main-thread call returns; a
            # silent return is not success.
            main.call(insert_image_result_on_sheet, img, doc=ctx.doc, image_index=i, image_path=path, image_count=len(images))
        except Exception as exc:
            log.warning("run_venv_python_script: plot insert failed: %s", exc)
            failures.append(exc)
            failed_paths.append(path)
            continue
        inserted_paths.append(path)
    _unlink_quiet(inserted_paths)
    _report_image_paths(out, failed_paths)
    if not failures:
        out["message"] = f"{len(images)} plot(s) inserted on active sheet"
        out["image_inserted"] = True
    elif not inserted_paths:
        out["status"] = "error"
        out["image_inserted"] = False
        out["plot_error"] = "; ".join(str(e) for e in failures)
        out["message"] = f"Plot was not inserted: {failures[0]}"
    else:
        out["image_inserted"] = True
        out["plot_error"] = "; ".join(str(e) for e in failures)
        out["message"] = f"{len(inserted_paths)} of {len(images)} plot(s) inserted on active sheet"
    return out


class RunVenvPythonScript(ToolCalcPythonBase):
    """Registered once; visible in Writer/Calc/Draw specialized ``domain=python`` via ``specialized_cross_cutting``."""

    name: str | None = "run_venv_python_script"
    specialized_cross_cutting: ClassVar[bool] = True
    description: str = f"Run Python code in the configured venv. {_RESULT_NOTE}"
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)
    long_running: bool = True

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        return _venv_schema_and_description(doc_type)[1]

    def get_description(self, doc_type: str | None = None) -> str:
        return _venv_schema_and_description(doc_type)[0]

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        code = str(kwargs.get("code", ""))
        py_data = None
        if ctx.doc_type == "calc":
            data_range = kwargs.get("data_range")
            data = kwargs.get("data")
            py_data, err = main.run(resolve_python_data, ctx.doc, data_range=data_range, data=data)
            if err:
                return self._tool_error(err, code="DATA_RESOLUTION_ERROR")

        # Pin ctx.doc for this call only. document_for_script_session
        # resolves a doc: id before the desktop's current component. The
        # pin is not the worker session id, so Isolated mode still gets a
        # fresh namespace. With no session id, host tool RPC uses
        # get_active_document and wa.draw / wa.shape bind to the front window.
        from plugin.scripting.session_manager import pin_script_document, release_script_document

        script_session_id = pin_script_document(ctx.doc)
        scope = ctx.send_cancellation

        try:
            res = run_code_in_user_venv(
                code,
                data=py_data,
                python_tool_domain=ctx.python_tool_domain,
                script_session_id=script_session_id,
                cancel=scope,
            )
        finally:
            release_script_document(script_session_id)

        if res.get("status") == "ok":
            images = find_image_payloads(res.get("result"))
            if images:
                return insert_plots_and_report(ctx, images, res)

        return res
