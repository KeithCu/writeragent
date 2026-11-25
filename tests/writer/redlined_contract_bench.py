# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read timings for a redlined contract, measured inside soffice on its main thread.

The test runner drives LibreOffice over a URP pipe, where every UNO call is a round trip,
so the same reads take 10 to 100 times longer than in the product. This file is loaded twice:
by the runner (``run_in_soffice``) and, as a Python UNO component, by soffice itself
(``Loader.activate`` in LibreOffice's ``pythonloader.py``).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import uno
import unohelper
from com.sun.star.awt import XCallback
from com.sun.star.beans import NamedValue, PropertyValue
from com.sun.star.task import XJob

IMPL_NAME = "org.extension.writeragent.tests.RedlinedContractBench"
REPO_ROOT = str(Path(__file__).resolve().parents[2])

# (label, tool name, arguments). "{table}" stands for the document's first table name.
OPS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("get_content full", "get_content", {}),
    ("search, 20 of many hits", "search", {"pattern": "Customer"}),
    ("get_tree heading_only", "get_tree", {"strategy": "heading_only", "depth": 0}),
    ("get_tree default", "get_tree", {}),
    ("table_get_cells", "table_get_cells", {"name": "{table}"}),
)


def run_in_soffice(ctx: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run *cases* inside soffice and return one result dict per case."""
    loader = ctx.getServiceManager().createInstanceWithContext("com.sun.star.loader.Python", ctx)
    factory = loader.activate(IMPL_NAME, "", uno.systemPathToFileUrl(__file__), None)
    raw = factory.createInstanceWithContext(ctx).execute((NamedValue("cases", json.dumps(cases)),))
    reply = json.loads(raw)
    if "error" in reply:
        raise AssertionError(reply["error"])
    return reply["results"]


def _use_checkout_plugin() -> None:
    """Make ``import plugin`` load this checkout.

    The runner seeds the throwaway profile with the user's installed WriterAgent
    (``testing_runner._seed_throwaway_profile_with_user_oxt``), and that copy is already
    imported in soffice when the job starts.
    """
    os.environ["WRITERAGENT_UNO_THREAD_GUARD"] = "0"  # read at import (plugin/framework/thread_guard.py); release bundles ship no guard
    for name in [n for n in sys.modules if n.split(".")[0] == "plugin"]:
        del sys.modules[name]
    sys.path.insert(0, REPO_ROOT)
    import plugin.doc.text_helpers as probe  # in-function: must follow the purge above

    assert probe.__file__.startswith(REPO_ROOT + os.sep), probe.__file__


def _tool_runner(ctx: Any) -> tuple[Any, Any]:
    from plugin import app  # in-function: only valid after _use_checkout_plugin
    from plugin.framework.config import init_config
    from plugin.framework.uno_context import set_fallback_ctx
    from plugin.main import bind

    set_fallback_ctx(ctx)
    init_config(ctx)
    if not app.is_bound():
        bind()
    app.bootstrap(ctx=ctx)
    return app.get_tools(), app.get_services()


def _run_case(ctx: Any, tools: Any, services: Any, case: dict[str, Any]) -> dict[str, Any]:
    from plugin.framework.tool import ToolContext  # in-function: only valid after _use_checkout_plugin

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    hidden = PropertyValue()
    hidden.Name, hidden.Value = "Hidden", True
    doc = desktop.loadComponentFromURL(case["url"], "_blank", 0, (hidden,))
    if doc is None:
        raise AssertionError(case["url"])
    try:
        doc.getDocumentIndexes().getByIndex(0).update()
        out: dict[str, Any] = {
            "name": case["name"],
            "pages": doc.getCurrentController().PageCount,
            "redlines": doc.getRedlines().getCount(),
            "timings": [],
        }
        tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=services, caller="test")
        first_table = doc.getTextTables().getElementNames()[0]
        for label, tool, args in OPS:
            if label not in case["ops"]:
                continue
            call = {k: (first_table if v == "{table}" else v) for k, v in args.items()}
            seconds: list[float] = []
            for _idx in range(case["repeats"]):
                start = time.perf_counter()
                result = tools.execute(tool, tctx, **call)
                seconds.append(time.perf_counter() - start)
                if result.get("status") != "ok":
                    raise AssertionError("%s: %r" % (label, result))
            out["timings"].append(
                {
                    "op": label,
                    "first": round(seconds[0], 3),
                    "median": round(statistics.median(seconds), 3),
                    "count": result.get("count"),
                    "warning": result.get("warning"),
                }
            )
        return out
    finally:
        doc.close(True)


class _MainThreadCall(unohelper.Base, XCallback):
    """Runs the cases on soffice's main thread and records one reply."""

    def __init__(self, ctx: Any, cases: list[dict[str, Any]]) -> None:
        self.ctx = ctx
        self.cases = cases
        self.reply: dict[str, Any] = {}
        self.done = threading.Event()

    def notify(self, _data: Any) -> None:
        try:
            _use_checkout_plugin()
            tools, services = _tool_runner(self.ctx)
            self.reply = {"results": [_run_case(self.ctx, tools, services, c) for c in self.cases]}
        except BaseException:
            self.reply = {"error": traceback.format_exc()}
        finally:
            self.done.set()


class RedlinedContractBench(unohelper.Base, XJob):
    """UNO XJob entry point that dispatches benchmark cases to soffice's main thread.

    Running directly on the calling URP thread hung once in two probes with every thread
    idle on a futex, and the product runs its tools on the main thread. ``done.wait()``
    has no timeout because the client test watchdog owns the timeout; adding one here
    would create a second outcome owner.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def execute(self, args: Any) -> str:
        cases = json.loads({a.Name: a.Value for a in args}["cases"])
        call = _MainThreadCall(self.ctx, cases)
        poster = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.AsyncCallback", self.ctx)
        poster.addCallback(call, None)
        call.done.wait()
        return json.dumps(call.reply)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(RedlinedContractBench, IMPL_NAME, ("com.sun.star.task.Job",))
