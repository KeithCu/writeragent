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


def get_examples_block(key: str) -> str:
    """Return the few-shot block for *key*.

    Specialized keys (``writer:shapes``, ``document_research:calc``, …) share
    ``DELEGATE_GENERIC_EXAMPLES_BLOCK`` so the DONE tool is always
    ``specialized_workflow_finished``.
    """
    if key == "librarian":
        return LIBRARIAN_EXAMPLES
    if key == "web_research":
        return WEB_RESEARCH_EXAMPLES_BLOCK
    return DELEGATE_GENERIC_EXAMPLES_BLOCK
