#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Long-lived venv worker: one JSON request per line on stdin, one response per line on stdout.

Each request runs user code in a **fresh** namespace (no cross-call globals). The process stays
warm; only interpreter state is discarded between requests.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import traceback
from typing import Any, IO


class Tee:
    def __init__(self, *files: IO[str]):
        self.files = files

    def write(self, obj: str):
        for f in self.files:
            f.write(obj)

    def flush(self):
        for f in self.files:
            if hasattr(f, "flush"):
                f.flush()


def _optional_module(name: str) -> Any | None:
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _serialize(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values."""
    np_mod = _optional_module("numpy")
    if np_mod is not None:
        if isinstance(obj, np_mod.ndarray):
            return obj.tolist()
        if isinstance(obj, (np_mod.integer,)):
            return int(obj)
        if isinstance(obj, (np_mod.floating,)):
            return float(obj)
        if isinstance(obj, np_mod.bool_):
            return bool(obj)
    pd_mod = _optional_module("pandas")
    if pd_mod is not None:
        if isinstance(obj, pd_mod.DataFrame):
            return obj.to_dict(orient="records")
        if isinstance(obj, pd_mod.Series):
            return obj.tolist()
    if isinstance(obj, (list, tuple)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    return obj


def _execute_request(code: str, data: Any | None) -> dict[str, Any]:
    """Run *code* in a new namespace; optional *data* is injected as a variable."""
    namespace: dict[str, Any] = {"__name__": "__main__"}
    if data is not None:
        namespace["data"] = data
    stdout_io = io.StringIO()
    is_console = os.environ.get("WRITERAGENT_SHOW_CONSOLE") == "1"
    
    # Save original stdin to restore after exec if we redirect it
    orig_stdin = sys.stdin
    if is_console:
        sys.stdin = sys.__stdin__

    try:
        targets: list[IO[str]] = [stdout_io]
        if is_console and sys.__stdout__ is not None:
            targets.append(sys.__stdout__)
        
        with contextlib.redirect_stdout(Tee(*targets)):
            compiled = compile(code, "<writeragent_worker>", "exec")
            exec(compiled, namespace, namespace)  # noqa: S102 — intentional user code in venv child
        result = namespace.get("result", namespace.get("_"))
        return {
            "status": "ok",
            "result": _serialize(result),
            "stdout": stdout_io.getvalue(),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": "".join(traceback.format_exception_only(type(e), e)).strip(),
            "traceback": traceback.format_exc(),
            "stdout": stdout_io.getvalue(),
        }
    finally:
        if is_console:
            sys.stdin = orig_stdin


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = ""
        try:
            request = json.loads(line)
            req_id = str(request.get("id", ""))
            code = request.get("code")
            if not isinstance(code, str) or not code.strip():
                response: dict[str, Any] = {"status": "error", "message": "No code provided."}
            else:
                response = _execute_request(code, request.get("data"))
        except json.JSONDecodeError as e:
            response = {"status": "error", "message": f"Invalid JSON request: {e}"}
        except Exception as e:
            response = {"status": "error", "message": str(e)}
        response["id"] = req_id
        sys.stdout.write(json.dumps(response, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
