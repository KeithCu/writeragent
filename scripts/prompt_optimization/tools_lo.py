# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Headless LibreOffice backend for prompt_optimization eval.

One soffice process; all UNO runs on ``_lo_thread`` via ``LOBackend.call``
(``-j`` is ThreadPoolExecutor in this process — do not ProcessPool one soffice).
Eval tools use ``bypass_thread_guard=True`` because ``_lo_thread`` is not
``threading.main_thread()``. Do not route through ``execute_on_main_thread``
(no VCL pump here → deadlock, same family as issue #402). Do not set
``WRITERAGENT_TESTING=1`` (QueueExecutor would go inline on the wrong thread).
"""
from __future__ import annotations

import glob
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

_lo_queue: queue.Queue[Any] = queue.Queue()
_lo_thread = None
_lo_ctx = None
_lo_desktop = None
# Keyed by caller thread id (set in task from LOBackend.call) so parallel model
# threads do not share a document. Each example still close+recreates.
_lo_docs: dict[int, Any] = {}
_lo_kinds: dict[int, str] = {}
_lo_proc = None  # headless soffice process, for cleanup
_lo_user_dir: str | None = None
# Thread-local on the LO worker: current caller's thread id for acquire_document().
_caller_tid_ctx = threading.local()

FACTORY_URLS = {
    "writer": "private:factory/swriter",
    "draw": "private:factory/sdraw",
    "calc": "private:factory/scalc",
}

# Eval-schema names → production ToolRegistry names.
_TOOL_ALIASES = {
    "write_cell_range": "write_formula_range",
}


def _caller_tid() -> int:
    if _lo_thread is not None and threading.get_ident() == _lo_thread.ident:
        return getattr(_caller_tid_ctx, "tid", None) or threading.get_ident()
    return threading.get_ident()


def _bootstrap_headless():
    """Start LibreOffice in headless mode and return the component context."""
    global _lo_proc, _lo_user_dir
    base = os.environ.get("UNO_PATH", "")
    soffice = os.path.join(base, "soffice")
    if sys.platform.startswith("win"):
        soffice += ".exe"
    if not os.path.isabs(soffice) and not soffice.startswith(os.sep):
        soffice = shutil.which("soffice") or shutil.which("soffice.exe") or soffice
    random.seed()
    pipe_name = "uno" + str(random.random())[2:]
    accept = f"--accept=pipe,name={pipe_name};urp;"
    _lo_user_dir = tempfile.mkdtemp(prefix="wa-eval-lo-")
    # file:// URL for a private profile so we do not lock the interactive user profile.
    user_url = "file://" + _lo_user_dir.replace(os.sep, "/")
    proc = subprocess.Popen(
        [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={user_url}",
            accept,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _lo_proc = proc
    try:
        import uno
        from com.sun.star.connection import NoConnectException

        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx
        )
        url = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                return resolver.resolve(url)
            except NoConnectException:
                time.sleep(0.5)
        raise RuntimeError("Cannot connect to soffice server (headless).")
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        raise


def _ensure_lo_python_path() -> None:
    lo_paths = [
        "/usr/lib/python3.14/site-packages",
        "/usr/lib/libreoffice/program",
    ]
    for base in ("/usr/lib/python3*/dist-packages", "/usr/lib/python3*/site-packages"):
        lo_paths.extend(glob.glob(base))
    for p in lo_paths:
        if p not in sys.path and os.path.exists(p):
            sys.path.append(p)


class LOBackend:
    @classmethod
    def start(cls):
        global _lo_thread, _lo_ctx, _lo_desktop
        if _lo_thread is not None:
            return

        _ensure_lo_python_path()
        # Headless eval: skip menu-icon preload (no VCL pump). Do not set
        # WRITERAGENT_TESTING=1 — that would inline QueueExecutor on the wrong thread.
        os.environ.setdefault("WRITERAGENT_EVAL_HARNESS", "1")
        # officehelper is optional if uno is already importable (venv + python3-uno).
        try:
            import officehelper  # noqa: F401
        except ImportError:
            try:
                import uno  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Could not find officehelper or uno. Are you sure LibreOffice is installed? "
                    "You may need to run make ensure-uno or install python3-uno."
                ) from exc

        _lo_ctx = _bootstrap_headless()
        smgr = _lo_ctx.getServiceManager()
        _lo_desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", _lo_ctx)

        try:
            from plugin.main import bootstrap

            bootstrap(_lo_ctx)
        except Exception:
            # Bootstrap can fail (missing _manifest) after soffice is already up.
            cls._cleanup()
            raise

        _lo_thread = threading.Thread(target=cls._worker_loop, daemon=True)
        _lo_thread.start()
        # Eval UNO lives on _lo_thread, not threading.main_thread(). Designate it
        # so the thread guard and QueueExecutor treat it as the UNO home thread
        # (inline on this thread — not execute_on_main_thread / VCL, not TESTING=1).
        from plugin.framework.thread_guard import set_designated_main_thread

        set_designated_main_thread(_lo_thread)

    @classmethod
    def stop(cls):
        global _lo_thread, _lo_user_dir
        if _lo_thread is None:
            return
        cls.call(cls._cleanup)
        _lo_queue.put(None)
        _lo_thread.join()
        _lo_thread = None
        from plugin.framework.thread_guard import set_designated_main_thread

        set_designated_main_thread(None)
        if _lo_user_dir:
            try:
                shutil.rmtree(_lo_user_dir, ignore_errors=True)
            except Exception:
                pass
            _lo_user_dir = None

    @classmethod
    def call(cls, func, *args, **kwargs):
        if _lo_thread is not None and threading.get_ident() == _lo_thread.ident:
            return func(*args, **kwargs)
        caller_tid = threading.get_ident()
        evt = threading.Event()
        result_box = []

        def _task():
            _caller_tid_ctx.tid = caller_tid
            try:
                result_box.append((True, func(*args, **kwargs)))
            except Exception as e:
                result_box.append((False, e))
            finally:
                _caller_tid_ctx.tid = None
            evt.set()

        _lo_queue.put(_task)
        evt.wait()
        success, res = result_box[0]
        if not success:
            raise res
        return res

    @classmethod
    def _worker_loop(cls):
        while True:
            task = _lo_queue.get()
            if task is None:
                break
            task()

    @classmethod
    def _cleanup(cls):
        global _lo_proc
        for doc in list(_lo_docs.values()):
            try:
                doc.close(True)
            except Exception:
                pass
        _lo_docs.clear()
        _lo_kinds.clear()
        try:
            _lo_desktop.terminate()
        except Exception:
            pass
        if _lo_proc is not None:
            try:
                _lo_proc.terminate()
                _lo_proc.wait(timeout=5)
            except Exception:
                pass
            _lo_proc = None

    @classmethod
    def current_kind(cls) -> str:
        return _lo_kinds.get(_caller_tid(), "writer")

    @classmethod
    def _create_factory(cls, kind: str):
        from com.sun.star.beans import PropertyValue

        url = FACTORY_URLS.get(kind) or FACTORY_URLS["writer"]
        hidden = PropertyValue()
        hidden.Name = "Hidden"
        hidden.Value = True
        return _lo_desktop.loadComponentFromURL(url, "_blank", 0, (hidden,))

    @classmethod
    def reset_document(cls, kind: str = "writer"):
        """Close the caller's doc and open a fresh factory document of ``kind``.

        Always private:factory — never reuse a dirty leftover or the interactive
        current document. Must run on ``_lo_thread`` (via ``call``).
        """
        if _lo_thread is not None and threading.get_ident() != _lo_thread.ident:
            return cls.call(cls.reset_document, kind)
        tid = _caller_tid()
        old = _lo_docs.pop(tid, None)
        if old is not None:
            try:
                old.close(True)
            except Exception:
                pass
        doc = cls._create_factory(kind)
        _lo_docs[tid] = doc
        _lo_kinds[tid] = kind
        return doc

    @classmethod
    def acquire_document(cls, kind: str | None = None):
        if _lo_thread is not None and threading.get_ident() != _lo_thread.ident:
            return cls.call(cls.acquire_document, kind)
        tid = _caller_tid()
        have = _lo_docs.get(tid)
        have_kind = _lo_kinds.get(tid)
        if have is not None and (kind is None or have_kind == kind):
            return have
        return cls.reset_document(kind or "writer")


def _tool_ctx(doc, doc_type: str = "writer"):
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services

    return ToolContext(doc, _lo_ctx, doc_type, get_services(), "eval")


def _coerce_calc_cell(raw: str) -> Any:
    text = (raw or "").strip()
    if text == "":
        return ""
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _parse_tsv_grid(text: str) -> list[list[Any]]:
    """Mirror CalcStringState TSV/CSV parse so string and lo start from the same grid."""
    grid: list[list[Any]] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if "\t" in stripped:
            cells = [cell.strip() for cell in stripped.split("\t")]
        else:
            cells = [cell.strip() for cell in stripped.split(",") if cell.strip()]
        if cells:
            grid.append([_coerce_calc_cell(c) for c in cells])
    return grid


def _set_writer_text(content: str) -> None:
    doc = LOBackend.acquire_document("writer")
    text = doc.getText()
    text.setString(content or "")


def _set_calc_tsv(content: str) -> None:
    doc = LOBackend.acquire_document("calc")
    grid = _parse_tsv_grid(content)
    if not grid:
        return
    width = max(len(row) for row in grid)
    padded = [list(row) + [""] * (width - len(row)) for row in grid]
    sheet = doc.getSheets().getByIndex(0)
    rng = sheet.getCellRangeByPosition(0, 0, width - 1, len(padded) - 1)
    rng.setDataArray(tuple(tuple(row) for row in padded))


def prepare_example(kind: str, initial_content: str) -> None:
    """Close leftover doc and create a fresh factory document for this example."""

    def _do():
        LOBackend.reset_document(kind)
        if kind == "writer":
            _set_writer_text(initial_content)
        elif kind == "calc":
            _set_calc_tsv(initial_content)
        # draw: empty page from private:factory/sdraw

    LOBackend.call(_do)


def set_document(content: str):
    """Writer-only helper (legacy DSPy / boot smoke). Prefer ``prepare_example``."""

    def _do():
        _set_writer_text(content)

    LOBackend.call(_do)


def get_content() -> str:
    def _do():
        doc = LOBackend.acquire_document()
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        return cursor.getString()

    return LOBackend.call(_do)


def _compact_writer_html(html: str) -> str:
    """Normalize Writer HTML so dataset substring checks survive LO export.

    LO XHTML pretty-prints (bulk_cleanup reject ``  ``), adds heading anchors
    (``<h1 data-lo-style=...><a id=...><span/></a>Introduction</h1>``), and may
    emit bold as ``<b>`` / font-weight instead of ``<strong>``.
    """
    compact = re.sub(r">\s+<", "><", html or "")
    stripped = re.sub(r"<(/?)(\w+)(\s[^>]*)>", r"<\1\2>", compact)

    def _heading(match: re.Match[str]) -> str:
        level = match.group(1)
        text = re.sub(r"<[^>]+>", "", match.group(2))
        return f"<h{level}>{text.strip()}</h{level}>"

    headings = re.sub(r"<h([1-6])>(.*?)</h\1>", _heading, stripped, flags=re.IGNORECASE | re.DOTALL)
    parts = [compact, stripped, headings]
    if re.search(r"<b[\s>]|<strong[\s>]|font-weight\s*:\s*bold", compact, re.IGNORECASE):
        parts.append("<strong>")
    return "\n".join(parts)


def get_content_as_html() -> str:
    """Return the Writer document as HTML (for eval judge). Empty string if export fails."""

    def _do():
        try:
            doc = LOBackend.acquire_document()
            from plugin.main import get_tools

            ctx = _tool_ctx(doc, "writer")
            res = get_tools().execute(
                "get_document_content",
                ctx,
                scope="full",
                bypass_thread_guard=True,
            )
            if isinstance(res, dict) and res.get("status") == "ok":
                return _compact_writer_html(res.get("content", "") or "")
            return ""
        except Exception:
            return ""

    return LOBackend.call(_do)


def get_document_content(scope="full", max_chars=None, start=None, end=None, **kwargs) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.main import get_tools

        params = {"scope": scope}
        if max_chars is not None:
            params["max_chars"] = max_chars
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        for k, v in kwargs.items():
            if v is not None:
                params[k] = v

        ctx = _tool_ctx(doc, "writer")
        res = get_tools().execute("get_document_content", ctx, bypass_thread_guard=True, **params)
        return json.dumps(res, ensure_ascii=False)

    return LOBackend.call(_do)


def apply_document_content(content: str, old_content: str = "", all_matches: bool = False, **kwargs) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.main import get_tools

        params = {"content": content, "old_content": old_content}
        if all_matches:
            params["all_matches"] = True
        for k, v in kwargs.items():
            if v is not None:
                params[k] = v
        ctx = _tool_ctx(doc, "writer")
        res = get_tools().execute("apply_document_content", ctx, bypass_thread_guard=True, **params)
        return json.dumps(res, ensure_ascii=False)

    return LOBackend.call(_do)


def find_text(search: str, start=0, limit=None, case_sensitive=True) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.writer.format import find_text_ranges

        try:
            ranges = find_text_ranges(doc, _lo_ctx, search, case_sensitive=case_sensitive)
            if limit:
                ranges = ranges[:limit]
            return json.dumps({"status": "ok", "ranges": ranges}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    return LOBackend.call(_do)


def _sheet_headers_and_used(doc) -> tuple[list[str], str]:
    sheet = doc.getSheets().getByIndex(0)
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    addr = cursor.getRangeAddress()
    from plugin.calc.address_utils import index_to_column

    used = (
        f"{index_to_column(addr.StartColumn)}{addr.StartRow + 1}:"
        f"{index_to_column(addr.EndColumn)}{addr.EndRow + 1}"
    )
    header_range = sheet.getCellRangeByPosition(
        addr.StartColumn, addr.StartRow, addr.EndColumn, addr.StartRow
    )
    data = header_range.getDataArray()
    headers: list[str] = []
    if data:
        for val in data[0]:
            headers.append("" if val == "" else str(val))
    return headers, used


def _read_sheet_grid(doc) -> list[list[Any]]:
    sheet = doc.getSheets().getByIndex(0)
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    raw = cursor.getDataArray()
    grid: list[list[Any]] = []
    for row in raw or ():
        out_row = []
        for cell in row:
            if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                out_row.append(float(cell))
            else:
                out_row.append(cell)
        grid.append(out_row)
    return grid


def normalize_lo_tool(
    name: str,
    args: dict[str, Any],
    *,
    kind: str = "writer",
    headers: list[str] | None = None,
    used_range: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map eval tool names/args to production ``get_tools().execute`` kwargs."""
    unused = kind
    del unused
    prod = _TOOL_ALIASES.get(name, name)
    params = dict(args or {})
    if "page_index" in params and "page" not in params:
        params["page"] = params.pop("page_index")

    if prod == "write_formula_range":
        rng = params.get("range")
        if isinstance(rng, str):
            params["range"] = [rng]
        values = params.get("values")
        if isinstance(values, (list, tuple)):
            params["values"] = json.dumps(list(values))
        elif isinstance(values, (int, float)) and not isinstance(values, bool):
            params["values"] = str(values)

    if prod == "sort_range":
        col = params.get("sort_column")
        if isinstance(col, str) and headers:
            try:
                params["sort_column"] = headers.index(col)
            except ValueError:
                params["sort_column"] = 0
        rng = params.get("range")
        if not rng and used_range:
            params["range"] = [used_range]
        elif isinstance(rng, str):
            params["range"] = [rng]

    return prod, params


def _execute_lo_tool_impl(name: str, args: dict[str, Any]) -> str:
    """Run one production tool on the LO thread. Used by unit tests with mocks."""
    from plugin.main import get_tools

    kind = LOBackend.current_kind()
    doc = LOBackend.acquire_document()
    headers = None
    used = None
    if kind == "calc" and name in ("sort_range", "write_cell_range", "write_formula_range", "get_sheet_summary"):
        try:
            headers, used = _sheet_headers_and_used(doc)
        except Exception:
            headers, used = None, None
    prod, params = normalize_lo_tool(name, args, kind=kind, headers=headers, used_range=used)
    ctx = _tool_ctx(doc, kind)
    res = get_tools().execute(prod, ctx, bypass_thread_guard=True, **params)
    if not isinstance(res, dict):
        res = {"status": "ok", "result": res}
    return json.dumps(res, ensure_ascii=False)


def execute_lo_tool(name: str, args: dict[str, Any], *, verbose: bool = False) -> str:
    if verbose:
        print(f"  [Tool] {name} {args}", flush=True)
    out = LOBackend.call(_execute_lo_tool_impl, name, args)
    if verbose:
        print(f"  [Tool->] {out[:500]!r}{'...' if len(out) > 500 else ''}", flush=True)
    return out


def get_draw_export() -> str:
    def _do():
        from plugin.main import get_tools

        doc = LOBackend.acquire_document()
        ctx = _tool_ctx(doc, "draw")
        res = get_tools().execute("get_draw_tree", ctx, bypass_thread_guard=True)
        return json.dumps(res, ensure_ascii=False, indent=2)

    return LOBackend.call(_do)


def get_calc_export() -> str:
    """Sheet summary + grid snapshot so Tax / 0.8 / snapshot can appear."""

    def _do():
        from plugin.main import get_tools

        doc = LOBackend.acquire_document()
        ctx = _tool_ctx(doc, "calc")
        summary = get_tools().execute("get_sheet_summary", ctx, bypass_thread_guard=True)
        grid = _read_sheet_grid(doc)
        headers = grid[0] if grid else []
        payload = {
            "status": "ok",
            "snapshot": True,
            "sheet": "Sheet1",
            "summary": summary,
            "headers": headers,
            "grid": grid,
            "rows": grid,
            "row_count": len(grid),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    return LOBackend.call(_do)


def get_eval_export(kind: str) -> str:
    if kind == "draw":
        return get_draw_export()
    if kind == "calc":
        return get_calc_export()
    return get_content_as_html() or ""


def dspy_get_document_content(scope: str = "full", max_chars: int = None, start: int = None, end: int = None) -> str:
    """Read the LibreOffice document. Returns JSON with content."""
    return get_document_content(scope, max_chars, start, end)


def dspy_apply_document_content(content: str, old_content: str = "", all_matches: bool = False) -> str:
    """Modify the LibreOffice document. Content can be plain text or HTML. Returns JSON status."""
    return apply_document_content(content, old_content, all_matches)


def dspy_find_text(search: str, start: int = 0, limit: int = None, case_sensitive: bool = True) -> str:
    """Search the LibreOffice document for text. Returns JSON with ranges."""
    return find_text(search, start, limit, case_sensitive)


VERBOSE = False


def with_logging(func, name):
    def wrapper(*args, **kwargs):
        if VERBOSE:
            print(f"  [Tool] {name} {args} {kwargs}")
        res = func(*args, **kwargs)
        if VERBOSE:
            print(f"  [Tool->] {res}")
        return res

    wrapper.__name__ = name
    wrapper.__doc__ = func.__doc__
    return wrapper


def get_tools_subset(names: list[str] | None = None):
    mapping = {
        "get_document_content": dspy_get_document_content,
        "apply_document_content": dspy_apply_document_content,
        "find_text": dspy_find_text,
    }
    if names is None:
        names = ["get_document_content", "apply_document_content", "find_text"]
    return [with_logging(mapping[n], n) for n in names if n in mapping]
