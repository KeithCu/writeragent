# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Smolagents few-shot example blocks (Action/Observation) used at runtime.

- **librarian** — onboarding (`reply_to_user`).
- **web_research** — web sub-agent (`final_answer`).
- **``*:python``** — venv demo (`run_venv_python_script` + ``sp.prime``; no numpy imports).
- **All other keys** — shared delegate demo (`specialized_workflow_finished`).

Refresh librarian text with: ``python scripts/generate_smol_examples.py``
"""

from __future__ import annotations

import logging

from plugin.contrib.smolagents.toolcalling_agent_prompts import DELEGATE_GENERIC_EXAMPLES_BLOCK, WEB_RESEARCH_EXAMPLES_BLOCK

log = logging.getLogger("writeragent.smol_examples")

LIBRARIAN_EXAMPLES = """Task: My name is Joe."

Action:
{
  "name": "upsert_memory",
  "arguments": {"key": "name", "content": "Joe"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "Hello, Joe! Would you like to learn more about WriterAgent?"}
}
Observation: {"status": "ok"}

"""

# Venv guidance for python few-shots (prepended to the block; not part of the Task: line).
PYTHON_SPECIALIZED_EXAMPLES_NOTES = """Example notes (venv Python): Prefer NumPy (np), SymPy (sp), and pandas (pd) over reimplementing algorithms in pure Python.
np, sp, pd, and math are already imported in the sandbox — do not write import lines for them.
A complete SciPy stack is available.

"""

# Specialized domain python (Writer/Calc/Draw): teach venv script shape + pre-imported np/sp/pd.
PYTHON_SPECIALIZED_EXAMPLES = (
    PYTHON_SPECIALIZED_EXAMPLES_NOTES
    + """Task: "What is the 100th prime number?"

Action:
{
  "name": "run_venv_python_script",
  "arguments": {"code": "result = sp.prime(100)\\n"}
}
Observation: {"status": "ok", "result": 541, "stdout": "", "stderr": ""}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "The 100th prime number is 541."}
}
"""
)


def get_examples_block(key: str) -> str:
    """Return the few-shot block for *key*.

    Specialized keys (``writer:shapes``, ``document_research:calc``, …) share
    ``DELEGATE_GENERIC_EXAMPLES_BLOCK`` so the DONE tool is always
    ``specialized_workflow_finished``. Keys ending in ``:python`` use
    ``PYTHON_SPECIALIZED_EXAMPLES``.
    """
    if key == "librarian":
        return LIBRARIAN_EXAMPLES
    if key == "web_research":
        return WEB_RESEARCH_EXAMPLES_BLOCK
    if key.endswith(":python"):
        return PYTHON_SPECIALIZED_EXAMPLES
    return DELEGATE_GENERIC_EXAMPLES_BLOCK
