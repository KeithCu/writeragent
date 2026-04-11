"""
DSPy program for Writer prompt optimization (MIPROv2 / run_optimize.py only).

Benchmarks use LlmClient + llm_chat_eval (run_eval.py); this module stays ReAct + tools_lo.
"""
from __future__ import annotations

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import dspy
from tools_lo import set_document, get_content_as_html, get_tools_subset

from plugin.framework.constants import get_writer_eval_chat_system_prompt

# Same rules/HTML contract as Writer chat; tools limited to tools_lo (see get_writer_eval_chat_system_prompt).
DEFAULT_CHAT_SYSTEM_PROMPT = get_writer_eval_chat_system_prompt()


class WriterAssistant(dspy.Module):
    """
    Writer assistant: document_context + user_question -> result.
    Uses mock get_document_content / apply_document_content / find_text.
    The instruction (system prompt) is what we optimize.
    """

    def __init__(self, instruction: str | None = None, tool_names: list[str] | None = None):
        super().__init__()
        self.instruction = instruction or DEFAULT_CHAT_SYSTEM_PROMPT
        tools = get_tools_subset(tool_names)
        # Signature: instructions is the system prompt that MIPROv2 can optimize.
        sig = dspy.Signature(
            "document_context, user_question -> result",
            instructions=self.instruction,
        )
        self.react = dspy.ReAct(sig, tools=tools, max_iters=10)

    def forward(self, document_content: str, user_question: str):
        set_document(document_content)
        # Pass document as context so the model can read it via get_document_content (or it's already in the prompt).
        document_context = document_content
        pred = self.react(document_context=document_context, user_question=user_question)
        html = get_content_as_html()
        if not html:
            raise RuntimeError("get_content_as_html() returned empty; document export failed.")
        pred.final_document = html
        return pred


def build_program(instruction: str | None = None, tool_names: list[str] | None = None) -> WriterAssistant:
    """Build a WriterAssistant program. tool_names=None uses core three tools."""
    return WriterAssistant(instruction=instruction, tool_names=tool_names)
