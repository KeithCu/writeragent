# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Score eval-1 task ``python_shapes_flag`` from the harness trace plus a Writer ``.odt``.

Headed eval-2 still reads ``writeragent_debug.log``. This path does not.
The string simulator cannot produce an honest trace (no ``run_venv``), so a
string-backend attempt fails closed before any geometry check.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_python_shapes_flag_oracle import (  # noqa: E402
    evidence_from_eval_trace,
    read_writer_flag,
    score_flag,
)

FLAG_TASK_ID = "python_shapes_flag"
# Headed helper writes chatbot.max_tool_rounds=50 for this Ask. The eval
# harness default is 25; bump only this row. Everyday chat stays 15.
FLAG_MAX_TOOL_ROUNDS = 50
_STRING_FAILURE = (
    "python_shapes_flag requires the headless LO backend; "
    "the string simulator has no run_venv_python_script"
)


@dataclass
class FlagScore:
    failures: list[str]
    soft: list[str] = field(default_factory=list)
    partial_score: float = 0.0
    summary: str = ""
    passed: bool = False


def score_flag_example(
    trace: list[dict] | None,
    *,
    backend: str,
    odt_path: str | None = None,
) -> FlagScore:
    """Hard path + page-scale stripes. Under-counted stars stay in ``soft``.

    ``odt_path`` is for tests. A live LO run leaves it unset and exports
    the current Writer document.
    """
    if backend != "lo":
        return FlagScore(
            failures=[_STRING_FAILURE],
            summary=_STRING_FAILURE,
        )
    geometry = None
    geometry_error: str | None = None
    exported: str | None = odt_path
    cleanup = False
    if not exported:
        try:
            import tools_lo

            exported = tools_lo.export_writer_odt()
            cleanup = True
        except Exception as exc:
            geometry_error = str(exc)
            exported = None
    try:
        if exported and geometry_error is None:
            try:
                geometry = read_writer_flag(Path(exported))
            except Exception as exc:
                geometry_error = str(exc)
        evidence = evidence_from_eval_trace(trace)
        # log_missing stays false: the harness trace is the process record.
        result = score_flag(
            geometry,
            evidence,
            geometry_error=geometry_error,
            log_missing=False,
        )
    finally:
        if cleanup and exported:
            try:
                Path(exported).unlink(missing_ok=True)
            except OSError:
                pass
    summary = (
        f"python_shapes_flag passed={result.passed} partial={result.partial_score:.3f} "
        f"stripes={result.stripe_rects} stars={result.star_shapes} "
        f"max_width_hmm={result.max_width_hmm}"
    )
    if result.soft:
        summary += " soft=" + "; ".join(result.soft)
    if result.failures:
        summary += " failures=" + "; ".join(result.failures)
    return FlagScore(
        failures=list(result.failures),
        soft=list(result.soft),
        partial_score=float(result.partial_score),
        summary=summary,
        passed=bool(result.passed),
    )
