# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Dispatch Python tool calls from user scripts to LibreOffice host or worker pipe."""

from __future__ import annotations

import os
from typing import Any

ROLE_HOST = "host"
ROLE_WORKER = "worker"
ROLE_COMPUTE = "compute"

_process_role: str | None = None


def set_process_role(role: str | None) -> None:
    """Set process role override ('host', 'worker', or 'compute')."""
    global _process_role
    _process_role = role


def get_process_role() -> str:
    """Return process role ('host', 'worker', or 'compute')."""
    if _process_role is not None:
        return _process_role
    if os.environ.get("WRITERAGENT_COMPUTE_WORKER") == "1":
        return ROLE_COMPUTE
    if os.environ.get("WRITERAGENT_IS_WORKER") == "1":
        return ROLE_WORKER
    return ROLE_HOST


def call_host_tool(tool_name: str, **kwargs: Any) -> Any:
    """Call a host tool either in-process or over the IPC pipe to LibreOffice.

    Strips None kwargs so tool defaults apply.
    """
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    role = get_process_role()
    if role == ROLE_COMPUTE:
        raise RuntimeError("WriterAgent document tools are not available in the Python compute service.")
    if role == ROLE_WORKER:
        from plugin.framework.ipc import exchange_tool_call

        return exchange_tool_call(tool_name, kwargs)

    try:
        from plugin.scripting.host_rpc import execute_tool

        return execute_tool(tool_name, kwargs, caller="script")
    except Exception as e:
        code = getattr(e, "code", None)
        err = RuntimeError(f"Failed to execute tool in-process: {e}")
        if code is not None:
            setattr(err, "code", code)
        raise err from e
