# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Run user Python via subprocess: configured venv, or ``sys.executable`` when venv path is empty (no UNO in child)."""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import tempfile
import time
from typing import Any, Dict, cast, IO

from plugin.framework.config import get_config_str
from plugin.scripting.venv_probe import resolve_libreoffice_python, resolve_venv_python

log = logging.getLogger(__name__)

_RESULT_PREFIX = "__WRITERAGENT_VENV_RESULT__"
_BLOCKED_ENV_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")


def scrub_subprocess_env(base: dict[str, str] | None) -> dict[str, str]:
    """Drop likely-secret vars from the environment passed to venv Python."""
    if not base:
        return {}
    out: dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if any(s in ku for s in _BLOCKED_ENV_SUBSTR):
            continue
        out[k] = v
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.setdefault("PYTHONUTF8", "1")
    out.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return out


def _build_runner_script(user_code: str, *, data: Any = None) -> str:
    """Build temp script: optional ``data`` preamble, user code, then JSON result trailer."""
    preamble = ""
    if data is not None:
        payload = json.dumps(data, default=str)
        preamble = "import json as _json\n" f"data = _json.loads({payload!r})\n"
    return (
        preamble
        + user_code.rstrip()
        + "\n\nimport json as _json\n"
        + "_wa = locals().get('result', locals().get('_'))\n"
        + f"print('{_RESULT_PREFIX}' + _json.dumps(_wa, default=str))\n"
    )


class VenvInteractiveRunner:
    """Manages a venv subprocess with an interactive JSON-RPC bridge for tool calls."""

    def __init__(self, exe: str, script_path: str, env: dict, timeout_sec: int, uno_ctx: Any, active_domain: str | None = None, python_tool_domain: str | None = None):
        self.exe = exe
        self.script_path = script_path
        self.env = env
        self.timeout_sec = timeout_sec
        self.uno_ctx = uno_ctx
        self.active_domain = active_domain
        self.python_tool_domain = python_tool_domain
        self.stdout_buf: list[str] = []
        self.stderr_buf: list[str] = []
        self.final_result: Any = None
        self._whitelist: frozenset[str] | None = None

    def _get_whitelist(self) -> frozenset[str] | None:
        """Return the set of allowed tool names for the current domain."""
        if self._whitelist is not None:
            return self._whitelist
        
        domain = self.python_tool_domain or self.active_domain
        if not domain:
            return None
            
        try:
            from plugin.scripting.writeragent_api import DOMAIN_TOOLS
            allowed = DOMAIN_TOOLS.get(domain)
            if allowed:
                self._whitelist = frozenset(allowed)
            else:
                self._whitelist = frozenset()
        except ImportError:
            log.warning("venv RPC: writeragent_api.py not found, whitelist disabled")
            return None
        except Exception:
            log.exception("venv RPC: failed to load whitelist")
            return None
            
        return self._whitelist

    def run(self) -> dict[str, Any]:
        """Run the process and handle RPC calls until exit."""
        # Add plugin/scripting to PYTHONPATH so the child can 'import writeragent_api'
        scripting_dir = os.path.dirname(os.path.abspath(__file__))
        existing_pp = self.env.get("PYTHONPATH", "")
        if existing_pp:
            self.env["PYTHONPATH"] = f"{scripting_dir}{os.pathsep}{existing_pp}"
        else:
            self.env["PYTHONPATH"] = scripting_dir

        start_time = time.time()
        try:
            proc = subprocess.Popen(
                [self.exe, self.script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                bufsize=1,  # Line buffered
            )
        except OSError as e:
            return {"status": "error", "message": f"Failed to spawn Python: {e}"}

        # Type hint narrowing for pipes
        stdout = cast("IO[str]", proc.stdout)
        stderr = cast("IO[str]", proc.stderr)

        try:
            while True:
                # Check for timeout
                if time.time() - start_time > self.timeout_sec:
                    proc.terminate()
                    return {"status": "error", "message": f"Timed out after {self.timeout_sec}s."}

                # Use select to wait for output from stdout or stderr
                ready, _, _ = select.select([stdout, stderr], [], [], 1.0)
                
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue

                if stdout in ready:
                    line = stdout.readline()
                    if not line:
                        break
                    
                    if line.startswith('{"type": "tool_call"'):
                        self._handle_tool_call(proc, line)
                    elif _RESULT_PREFIX in line:
                        idx = line.index(_RESULT_PREFIX) + len(_RESULT_PREFIX)
                        payload = line[idx:].strip()
                        try:
                            self.final_result = json.loads(payload)
                        except json.JSONDecodeError:
                            log.warning("venv run: bad JSON after sentinel: %s", payload[:200])
                    else:
                        self.stdout_buf.append(line)

                if stderr in ready:
                    line = stderr.readline()
                    if line:
                        self.stderr_buf.append(line)

            # Wait for exit
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"status": "error", "message": "Timed out during cleanup."}
        except Exception as e:
            proc.kill()
            log.exception("Unexpected error in interactive runner")
            return {"status": "error", "message": f"Internal error: {e}"}

        stdout_full = "".join(self.stdout_buf)
        stderr_full = "".join(self.stderr_buf).strip()

        if proc.returncode != 0:
            tail = stderr_full or stdout_full.strip()
            tail = tail[:800] + ("…" if len(tail) > 800 else "")
            msg = f"Python exited with code {proc.returncode}."
            if tail:
                msg = f"{msg}\n{tail}"
            return {"status": "error", "message": msg, "stdout": stdout_full.strip(), "stderr": stderr_full}

        return {
            "status": "ok",
            "result": self.final_result,
            "stdout": stdout_full.strip(),
            "stderr": stderr_full,
        }

    def _handle_tool_call(self, proc: subprocess.Popen, line: str):
        """Parse tool call, execute it, and write response to child's stdin."""
        stdin = cast("IO[str]", proc.stdin)
        try:
            call = json.loads(line)
            tool_name = call.get("tool")
            args = call.get("args", {})
            call_id = call.get("id")

            log.debug("venv RPC: tool_call %s(%s)", tool_name, args)

            # Whitelist enforcement
            whitelist = self._get_whitelist()
            if whitelist is not None and tool_name not in whitelist:
                log.warning("venv RPC: REJECTED tool %s (active_domain=%s, python_tool_domain=%s)", tool_name, self.active_domain, self.python_tool_domain)
                response = {"id": call_id, "status": "error", "message": f"Access denied: tool '{tool_name}' is not allowed"}
                stdin.write(json.dumps(response) + "\n")
                stdin.flush()
                return

            from plugin.main import get_tools
            from plugin.framework.tool import ToolContext
            from plugin.framework.uno_context import get_active_document
            
            doc = get_active_document(self.uno_ctx)
            # Infer doc_type
            from plugin.doc.document_helpers import get_document_type, DocumentType
            doc_type_enum = get_document_type(doc)
            doc_type = "writer"
            if doc_type_enum == DocumentType.CALC: doc_type = "calc"
            elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS): doc_type = "draw"

            tctx = ToolContext(
                doc=doc,
                ctx=self.uno_ctx,
                doc_type=doc_type,
                services=get_tools()._services,
                caller="python_venv",
                active_domain=self.active_domain,
                python_tool_domain=self.python_tool_domain,
            )
            
            try:
                result = get_tools().execute(tool_name, tctx, **args)
                response = {"id": call_id, "status": "ok", "result": result}
            except Exception as e:
                log.exception("venv RPC: tool execution failed")
                response = {"id": call_id, "status": "error", "message": str(e)}

            stdin.write(json.dumps(response) + "\n")
            stdin.flush()
        except Exception as e:
            log.exception("venv RPC: internal bridge error")
            try:
                stdin.write(json.dumps({"status": "error", "message": f"Bridge error: {e}"}) + "\n")
                stdin.flush()
            except Exception:
                pass


def run_code_in_user_venv(
    uno_ctx: Any,
    code: str,
    *,
    data: Any = None,
    timeout_sec: int = 120,
    active_domain: str | None = None,
    python_tool_domain: str | None = None,
) -> Dict[str, Any]:
    """Execute *code* in the configured venv, or in ``sys.executable`` when the venv path is empty."""
    if not (code or "").strip():
        return {"status": "error", "message": "No code provided."}

    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return {
                "status": "error",
                "message": f"No python executable found under configured venv: {venv_dir!r}",
            }
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
    else:
        exe = resolve_libreoffice_python()
        if not exe:
            return {
                "status": "error",
                "message": (
                    "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
                    "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install."
                ),
            }
        log.debug("run_venv_code: using process interpreter %s (no venv path set)", exe)

    if timeout_sec < 1:
        timeout_sec = 1
    if timeout_sec > 600:
        timeout_sec = 600

    script_body = _build_runner_script(code, data=data)
    child_env = scrub_subprocess_env(dict(os.environ))

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(script_body)
            tmp_path = tmp.name
    except OSError as e:
        return {"status": "error", "message": f"Could not create temp script: {e}"}

    try:
        runner = VenvInteractiveRunner(exe, tmp_path, child_env, timeout_sec, uno_ctx, active_domain=active_domain, python_tool_domain=python_tool_domain)
        return runner.run()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
