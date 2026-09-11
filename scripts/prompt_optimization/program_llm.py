# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DSPy student whose forward is the live string-harness tool loop.

MIPROv2 mutates a dummy predictor's ``instructions`` (the named slice).
``forward`` never calls that predictor — ReAct stays offline. The live
path is ``apply_named_slice`` + ``run_llm_chat_eval`` (same as run_eval).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
for _p in (REPO_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dspy

from optimize_slices import (
    DEFAULT_LLM_SLICE,
    apply_named_slice,
    get_slice_baseline,
)
from llm_chat_eval import run_llm_chat_eval


class LiveEvalStudent(dspy.Module):
    """One string-harness example → ``final_document`` (+ usage for the metric)."""

    def __init__(
        self,
        *,
        slice_name: str = DEFAULT_LLM_SLICE,
        slice_text: str | None = None,
        endpoint: str = "",
        api_key: str = "",
        model: str = "",
        backend: Literal["string", "lo"] = "string",
        max_tool_rounds: int = 25,
        student: Literal["llm", "scripted"] = "llm",
        verbose: bool = False,
        prompt_task_id: str = "",
    ) -> None:
        super().__init__()
        self.slice_name = slice_name
        baseline = slice_text if slice_text is not None else get_slice_baseline(
            slice_name, prompt_task_id=prompt_task_id
        )
        self.baseline_slice_text = baseline
        self.baseline_slice_len = len(baseline)
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.backend = backend
        self.max_tool_rounds = max_tool_rounds
        self.student = student
        self.verbose = verbose
        self.prompt_task_id = prompt_task_id
        # Dummy predictor: MIPROv2 instruction-only search rewrites this text.
        # Do not call ``propose`` — that would be a second (DSPy) student.
        sig = dspy.Signature(
            "document_content, user_question, task_id -> result",
            instructions=baseline,
        )
        self.propose = dspy.Predict(sig)

    def get_slice_text(self) -> str:
        sig = getattr(self.propose, "signature", None)
        text = getattr(sig, "instructions", None)
        if isinstance(text, str) and text:
            return text
        return self.baseline_slice_text

    def forward(self, document_content: str, user_question: str, task_id: str = ""):
        slice_text = self.get_slice_text()
        system_prompt, schema_patches = apply_named_slice(
            task_id,
            self.slice_name,
            slice_text,
            prompt_task_id=self.prompt_task_id,
        )
        final, usage, error, _trace = run_llm_chat_eval(
            system_prompt=system_prompt,
            document_content=document_content,
            user_question=user_question,
            endpoint=self.endpoint,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            max_tool_rounds=self.max_tool_rounds,
            bust_cache=False,
            verbose=self.verbose,
            student=self.student,
            task_id=task_id,
            schema_patches=schema_patches,
        )
        prompt_tok = int(usage.get("prompt_tokens", 0))
        completion_tok = int(usage.get("completion_tokens", 0))
        total_tok = int(usage.get("total_tokens", 0))
        if total_tok == 0 and (prompt_tok or completion_tok):
            total_tok = prompt_tok + completion_tok
        # get_lm_usage shape matches metric._get_total_tokens (DSPy ReAct path).
        usage_payload = {
            self.model or "llm_chat_eval": {
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion_tok,
                "total_tokens": total_tok,
            }
        }
        pred = dspy.Prediction(
            result=final,
            final_document=final,
            total_tokens=total_tok,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            slice_text=slice_text,
            baseline_slice_len=self.baseline_slice_len,
            error=error,
        )
        pred.get_lm_usage = lambda: usage_payload  # type: ignore[method-assign]
        return pred


def build_live_program(**kwargs: Any) -> LiveEvalStudent:
    return LiveEvalStudent(**kwargs)
