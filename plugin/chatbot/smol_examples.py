# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Smolagents few-shot example blocks (Action/Observation) used at runtime.

- **librarian** — onboarding (`reply_to_user`).
- **brainstorming** — design exploration (`reply_to_user`, `save_design_spec`, `brainstorming_finished`).
- **web_research** — web sub-agent (`final_answer`).
- **``*:python``** — venv demo (`run_venv_python_script` + ``sp.prime``; no numpy imports).
- **All other keys** — shared delegate demo (`specialized_workflow_finished`).

Refresh librarian text with: ``python scripts/generate_smol_examples.py``
"""

from __future__ import annotations

import logging

from plugin.contrib.smolagents.toolcalling_agent_prompts import DELEGATE_GENERIC_EXAMPLES_BLOCK, WEB_RESEARCH_EXAMPLES_BLOCK
from plugin.scripting.import_policy import format_venv_import_policy_for_prompt

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

BRAINSTORMING_EXAMPLES = """Task: "Brainstorm a sidebar export feature for WriterAgent."

Action:
{
  "name": "get_document_tree",
  "arguments": {}
}
Observation: {"status": "ok", "tree": "..."}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>What file format should export support first — PDF only, or Office formats too?</p>"}
}
Observation: {"status": "ok"}

Task: "Office formats too, keep it simple."

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p><strong>Recommended:</strong> export the active Writer document only, via UNO save APIs.</p><ul><li><strong>Recommended — sidebar button:</strong> one click, uses current doc path; smallest scope.</li><li><strong>Batch folder export:</strong> more UX work; defer unless you need many files.</li><li><strong>Cloud upload:</strong> needs auth and networking; out of scope for v1.</li></ul><p>Does the recommended sidebar-button approach work for you?</p>"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<h2>Architecture</h2><p>Sidebar Send handler calls a new export tool on the main thread; the tool resolves the active frame document and invokes UNO store APIs. Errors surface as HTML in chat.</p><p>Does this architecture section look right so far?</p>"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>Self-review: I removed a vague TBD about cloud sync and tightened the testing section before saving.</p>"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "save_design_spec",
  "arguments": {
    "content": ["<h1>Design: Sidebar Export</h1>", "<h2>Goals</h2>", "<ul><li>One-click export from chat sidebar</li></ul>", "<h2>Architecture</h2>", "<p>Main-thread UNO export from the active Writer document.</p>", "<h2>Testing</h2>", "<ul><li>Unit test export tool with mocked document</li><li>UNO smoke test on sample .odt</li></ul>"],
    "target": "end"
  }
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>I've saved the design spec at the end of your document. Please read it there and tell me if you want any changes before implementation.</p>"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "brainstorming_finished",
  "arguments": {"message": "<p>Spec saved at the end of your document. Ready to implement when you are.</p>", "spec_saved": true}
}
Observation: {"status": "finished"}

"""

# Venv guidance for python few-shots (prepended to the block; not part of the Task: line).
PYTHON_SPECIALIZED_EXAMPLES_NOTES = format_venv_import_policy_for_prompt(compact=True) + "\n\n"

# Specialized domain python (Writer/Calc/Draw): teach venv script shape + pre-imported np/sp/pd.
PYTHON_SPECIALIZED_EXAMPLES = (
    PYTHON_SPECIALIZED_EXAMPLES_NOTES
    + """Task: "Calculate prime numbers from the 1010th prime to the 1020th prime as a NumPy array."

Action:
{
  "name": "run_venv_python_script",
  "arguments": {"code": "low, high = sp.prime(1010), sp.prime(1020)\\nresult = np.array(sp.primerange(low, high + 1))\\n"}
}
Observation: {"status": "ok", "result": [8017, 8039, 8053, 8059, 8069, 8081, 8087, 8089, 8093, 8101, 8111], "stdout": "", "stderr": ""}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "I have successfully calculated the prime numbers and stored them in the `result` variable as a NumPy array. The values are: [8017, 8039, 8053, 8059, 8069, 8081, 8087, 8089, 8093, 8101, 8111]."}
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
    if key == "brainstorming":
        return BRAINSTORMING_EXAMPLES
    if key == "web_research":
        return WEB_RESEARCH_EXAMPLES_BLOCK
    if key.endswith(":python"):
        return PYTHON_SPECIALIZED_EXAMPLES
    return DELEGATE_GENERIC_EXAMPLES_BLOCK
