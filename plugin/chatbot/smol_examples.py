# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Smolagents few-shot example blocks (Action/Observation) used at runtime.

- **librarian** — onboarding (`reply_to_user`; leave with ``switch_to_document_mode=true``).
- **brainstorming** — design exploration (`reply_to_user`, `save_design_spec`; leave with ``brainstorming_finished=true``).
- **deep_research** — multi-step web research + `apply_document_content` (`reply_to_user`, `deep_research_web`).
- **web_research** — web sub-agent (`final_answer`; ``web_search`` arguments are ``{"query": "..."}``).
- **ppt-master** — venv PPT-Master agent (`ppt_master_finished`).
- **``*:python``** — outer python agent: venv demo (`run_venv_python_script` + ``sp.prime``), footnotes via ``delegate_tool_domains``, and a ring of shapes (``delegate_tool_domains`` whose task tells the inner agent to place circles with one script for-loop over ``wa.shape.upsert``, then finish). Hand-written here, not from ``generate_smol_examples.py``.
- **``domain_tools:shapes``** — inner agent after that hop: one ``run_venv_python_script`` with the for-loop, then ``specialized_workflow_finished``. Not the outer ``*:python`` block (that block calls ``delegate_tool_domains``, which the inner list does not include).
- **``*:images``** — edit selected image via ``image_generate(source_image='selection')``.
- **All other keys** — shared delegate demo (`specialized_workflow_finished`).

Refresh librarian text with: ``python scripts/generate_smol_examples.py``
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.contrib.smolagents.toolcalling_agent_prompts import DELEGATE_GENERIC_EXAMPLES_BLOCK, WEB_RESEARCH_EXAMPLES_BLOCK
from plugin.scripting.import_policy import format_venv_import_policy_for_prompt

log = logging.getLogger("writeragent.smol_examples")

LIBRARIAN_EXAMPLES = """Task: User confirmed they want to be called Joe after the librarian asked "Would you like me to call you Joe?"

Action:
{
  "name": "upsert_memory",
  "arguments": {"key": "name", "content": "Joe"}
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "Great, Joe! Would you like to learn more about WriterAgent?"}
}
Observation: {"status": "ok"}

Task: User said they want to work on a spreadsheet now.

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "Sounds good — switching you to the document assistant.", "switch_to_document_mode": true}
}
Observation: {"status": "switch_mode"}

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
  "name": "reply_to_user",
  "arguments": {"answer": "<p>Spec saved at the end of your document. Ready to implement when you are.</p>", "brainstorming_finished": true, "spec_saved": true}
}
Observation: {"status": "finished"}


"""

WRITING_PLAN_EXAMPLES = """Task: "Write a short article about LibreOffice history."

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>I will help you write a short article. Here is a proposed Writing Plan/Outline:</p><ol><li><strong>Section 1: Origin and Fork</strong> (covering the OOo history)</li><li><strong>Section 2: The Document Foundation era</strong></li><li><strong>Section 3: Community growth and modern status</strong></li></ol><p>Does this outline look good to start writing?</p>"}
}
Observation: {"status": "ok"}

Task: "Yes, start writing."

Action:
{
  "name": "write_document_section",
  "arguments": {
    "content": ["<h2>Section 1: Origin and Fork</h2>", "<p>LibreOffice was forked from OpenOffice.org in 2010 due to concerns over Oracle's acquisition of Sun Microsystems.</p>"],
    "target": "end"
  }
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>I've written Section 1 (Origin and Fork). Should I proceed to Section 2 (TDF Era)?</p>"}
}
Observation: {"status": "ok"}

Task: "Go ahead"

Action:
{
  "name": "write_document_section",
  "arguments": {
    "content": ["<h2>Section 2: TDF Era</h2>", "<p>The Document Foundation was created to host and guide the development of LibreOffice in a community-driven manner.</p>"],
    "target": "end"
  }
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>Writing plan execution completed. Sections 1 & 2 have been successfully drafted in the document.</p>", "writing_plan_finished": true}
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

Task: "Add a footnote after the sentence about revenue."

Action:
{
  "name": "delegate_tool_domains",
  "arguments": {"domains": ["footnotes"], "task": "Insert a footnote after the sentence about revenue with the source citation."}
}
Observation: {"status": "ok", "domains": ["footnotes"], "result": "Inserted the footnote."}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "Added the footnote via the footnotes domain."}
}

Task: "Use python to place a ring of 8 blue circles."

Action:
{
  "name": "delegate_tool_domains",
  "arguments": {"domains": ["shapes"], "task": "Place a ring of 8 blue circles with one run_venv_python_script. import writeragent as wa and use a Python for-loop that calls wa.shape.upsert for each circle. Page-scale absolute positions in HMM (1/100 mm). Origin is the top-left of the page, not (0,0) as a center. Page center about (10500, 14000), radius 7000 HMM, each circle 1600 HMM across, fill blue. Do not call a per-shape LLM tool."}
}
Observation: {"status": "ok", "domains": ["shapes"], "result": "Placed 8 blue circles in a ring."}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "Placed a ring of 8 blue circles via the shapes domain."}
}
"""
)

# Inner agent after delegate_tool_domains(domains=["shapes"]).
# One script loops the allowlisted shape API; per-shape LLM tools are not on this list.
INNER_SHAPES_SCRIPT_EXAMPLES = """Task: "Place a ring of 8 blue circles. Page-scale absolute positions in HMM (1/100 mm). Origin is the top-left of the page, not (0,0). Center about (10500, 14000), radius 7000 HMM, each circle 1600 HMM across, fill blue. Use one script for-loop."

Action:
{
  "name": "run_venv_python_script",
  "arguments": {"code": "import writeragent as wa\\ncx, cy, r, d = 10500, 14000, 7000, 1600\\nangles = np.linspace(0, 2 * np.pi, 8, endpoint=False)\\nfor a in angles:\\n    wa.shape.upsert('create', shape_type='ellipse', x=int(cx + np.cos(a) * r - d / 2), y=int(cy + np.sin(a) * r - d / 2), width=d, height=d, fill_color='blue')\\nresult = 'placed 8'\\n"}
}
Observation: {"status": "ok", "result": "placed 8", "stdout": "", "stderr": ""}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "Placed a ring of 8 blue circles with one script looping wa.shape.upsert."}
}
"""


# Images specialist: edit-in-place uses image_generate(source_image='selection')
# → selection b64 → img2img → replace_image_in_place. A delete + prompt-only
# generate creates a new graphic and skips that path.
IMAGES_SPECIALIZED_EXAMPLES = """Task: "Make it look like a wizard"

Action:
{
  "name": "image_generate",
  "arguments": {"prompt": "make it look like a wizard", "source_image": "selection"}
}
Observation: {"status": "ok", "message": "Image edited in place."}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "Updated the selected image in place so it looks like a wizard."}
}

Task: "Generate a tabby cat"

Action:
{
  "name": "image_generate",
  "arguments": {"prompt": "a tabby cat"}
}
Observation: {"status": "ok", "message": "Image generated successfully."}

Action:
{
  "name": "specialized_workflow_finished",
  "arguments": {"answer": "Inserted a new tabby cat image."}
}
"""


PPT_MASTER_EXAMPLES = """Task: "Export my ppt-master project at ~/projects/demo to this deck."

Action:
{
  "name": "validate_ppt_master_project",
  "arguments": {"project_path": "/home/user/projects/demo"}
}
Observation: {"status": "ok", "has_svg": true}

Action:
{
  "name": "export_presentation_project",
  "arguments": {"project_path": "/home/user/projects/demo"}
}
Observation: {"status": "ok", "slides": 5}

Action:
{
  "name": "ppt_master_finished",
  "arguments": {"message": "<p>Exported 5 slides as native Impress shapes.</p>", "exported": true}
}
Observation: {"status": "finished"}


"""

DEEP_RESEARCH_EXAMPLES = """Task: "Research recent advances in solid-state batteries and add a summary to my document."

Action:
{
  "name": "deep_research_web",
  "arguments": {"query": "solid-state battery commercialization advances 2024 2025"}
}
Observation: {"status": "ok", "result": "Plain text report with findings..."}

Action:
{
  "name": "apply_document_content",
  "arguments": {
    "content": ["<h1>Solid-State Batteries</h1>", "<p>Summary paragraph...</p>", "<ul><li>Key finding one</li></ul>"],
    "target": "end"
  }
}
Observation: {"status": "ok"}

Action:
{
  "name": "reply_to_user",
  "arguments": {"answer": "<p>I completed deep web research on solid-state batteries and appended a formatted summary to the end of your document.</p>"}
}
Observation: {"status": "ok"}

"""


def get_examples_block(key: str) -> str:
    """Return the few-shot block for *key*.

    Specialized keys (``writer:shapes``, ``document_research:calc``, …) share
    ``DELEGATE_GENERIC_EXAMPLES_BLOCK`` so the DONE tool is always
    ``specialized_workflow_finished``. Keys ending in ``:python`` use
    ``PYTHON_SPECIALIZED_EXAMPLES`` (outer agent). ``domain_tools:shapes`` uses
    ``INNER_SHAPES_SCRIPT_EXAMPLES`` (inner agent after ``delegate_tool_domains``).
    Keys ending in ``:images`` use ``IMAGES_SPECIALIZED_EXAMPLES``.
    """
    if key == "librarian":
        return LIBRARIAN_EXAMPLES
    if key == "brainstorming":
        return BRAINSTORMING_EXAMPLES
    if key == "writing_plan":
        return WRITING_PLAN_EXAMPLES
    if key == "deep_research":
        return DEEP_RESEARCH_EXAMPLES
    if key == "ppt-master":
        return PPT_MASTER_EXAMPLES
    if key == "web_research":
        return WEB_RESEARCH_EXAMPLES_BLOCK
    if key == "domain_tools:shapes":
        return INNER_SHAPES_SCRIPT_EXAMPLES
    if key.endswith(":python"):
        return PYTHON_SPECIALIZED_EXAMPLES
    if key.endswith(":images"):
        return IMAGES_SPECIALIZED_EXAMPLES
    return DELEGATE_GENERIC_EXAMPLES_BLOCK


def normalize_html_content_array(content: Any) -> list[str] | None:
    """Accept list of HTML strings or a single string (coerce to one-element list)."""
    if content is None:
        return None
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else None
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out if out else None
    return None

