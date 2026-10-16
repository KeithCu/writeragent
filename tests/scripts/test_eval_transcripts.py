# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval CLIs always save transcripts: scripted student writes one JSON per task (no API key)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
for p in (str(_PO), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm_chat_eval  # noqa: E402
from dataset import ALL_EXAMPLES  # noqa: E402
from eval_prompts import get_eval_system_prompt  # noqa: E402


def _run(task_id: str):
    ex = next(e for e in ALL_EXAMPLES if e["task_id"] == task_id)
    return llm_chat_eval.run_llm_chat_eval(
        system_prompt=get_eval_system_prompt(task_id),
        document_content=ex["document_content"],
        user_question=ex["user_question"],
        endpoint="http://unused",
        api_key="",
        model="vendor/model:tag",
        student="scripted",
        task_id=task_id,
    )


def test_transcript_written_when_enabled(tmp_path):
    llm_chat_eval.set_transcript_dir(str(tmp_path))
    try:
        final, _usage, err, trace = _run("table_from_mess")
    finally:
        llm_chat_eval.set_transcript_dir(None)
    assert err is None
    path = tmp_path / "vendor_model_tag" / "table_from_mess.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = [m["role"] for m in data["messages"]]
    assert roles[:2] == ["system", "user"]
    assert "assistant" in roles and "tool" in roles
    assert any(m.get("tool_calls") for m in data["messages"] if m["role"] == "assistant")
    assert data["trace"] == trace and trace
    assert data["final_document"] == final
    assert data["error"] is None
    assert len(data["rounds"]) >= 1


def test_enable_default_transcripts_uses_timestamped_dir(tmp_path):
    path = llm_chat_eval.enable_default_transcripts(str(tmp_path))
    try:
        assert Path(path).parent == tmp_path
        _run("table_from_mess")
    finally:
        llm_chat_eval.set_transcript_dir(None)
    assert (Path(path) / "vendor_model_tag" / "table_from_mess.json").is_file()


def test_default_root_is_gitignored_transcripts_dir():
    assert Path(llm_chat_eval.TRANSCRIPTS_ROOT) == _PO / "transcripts"
    assert "scripts/prompt_optimization/transcripts/" in (_REPO / ".gitignore").read_text()
