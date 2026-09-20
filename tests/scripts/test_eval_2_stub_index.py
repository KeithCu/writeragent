# WriterAgent tests for eval-2 sibling stubs
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stub folders stay light: required files, no fake gold, no product internals."""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_EVAL2 = _REPO / "docs" / "eval" / "eval-2"
_README = _EVAL2 / "README.md"

# Ready siblings already have headed helper + oracle. Slot 7 stays PARKED.
_READY = (
    "tenant-retention-ed2bc14c",
    "cadaver-proposal-61b0946a",
    "afc-sample-83d10b06",
    "gmp-change-control-58ac1cc5",
    "writer-calc-peer-write",
    "calc-primary-model",
    "draw-primary-deliverable",
    "reverse-tenant",
    "long-writer-pack",
)
_STUBS = (
    "writer-headed-template",
)
# Only the parked letterhead stub still points at an unused gold tree.
_GOLD_STUBS = {
    "writer-headed-template": "a46d5cd2-55fe-48fa-a4c6-6aaf6b9991b5",
}
_READY_GOLD_IDS = (
    "ed2bc14c-99ac-4a2a-8467-482a1a5d67f3",
    "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0",
    "83d10b06-26d1-4636-a32c-23f92c57f30b",
    "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
    "c3525d4d-2012-45df-853e-2d2a0e902991",
    "5f6c57dd-feb6-4e70-b152-4969d92d1608",
    "8a7b6fca-60cc-4ae3-b649-971753cbf8b9",
    "4520f882-715a-482d-8e87-1cb3cbdfe975",
)
_STUB_FILES = (
    "notes.md",
    "SOURCE.md",
    "run.md",
    "prompt.writeragent.txt",
    "rubric.eval2.md",
    "fixtures/.gitkeep",
    "runs/.gitkeep",
)
_BANNED_PRODUCT_INTERNALS = (
    "send_peer_work",
    "send_peer_result",
    "fill_draw_fields",
    "document_research",
    "specialized_workflow_finished",
    "peer_inner",
)
_BANNED_STEERING = (
    "don't invent",
    "do not invent",
    "don't make up",
    "from knowledge",
    "web not required",
    "already named",
)


def test_readme_lists_siblings_1_to_10() -> None:
    text = _README.read_text(encoding="utf-8")
    for slug in _READY + _STUBS:
        assert f"`{slug}/`" in text or f"({slug}/)" in text, slug
    assert "Ready" in text or "Headed-ready" in text
    assert "Stub" in text
    assert "PARKED" in text
    assert "7 stays PARKED" in text or "slot 7 stays PARKED" in text.lower()
    assert "not wired" in text
    for gold_id in (*_GOLD_STUBS.values(), *_READY_GOLD_IDS):
        assert gold_id in text, gold_id
    assert "Needs gold materials" not in text
    assert "long-writer-pack" in text
    assert "native" in text.lower()


def test_stub_folders_have_required_files() -> None:
    for slug in _STUBS:
        root = _EVAL2 / slug
        for rel in _STUB_FILES:
            path = root / rel
            assert path.is_file(), path


def test_golded_stub_source_points_at_untouched_tree() -> None:
    """Parked slot 7 keeps an untouched gdpval tree; it is not Ready."""
    gdpval = _REPO / "docs" / "eval" / "gdpval"
    for slug, gold_id in _GOLD_STUBS.items():
        source = (_EVAL2 / slug / "SOURCE.md").read_text(encoding="utf-8")
        assert gold_id in source, slug
        assert f"docs/eval/gdpval/{gold_id}/" in source, slug
        assert "Needs gold materials" not in source, slug
        gold_tree = gdpval / gold_id
        assert gold_tree.is_dir(), gold_tree
        gold_prompt = gold_tree / "prompt.txt"
        exp_prompt = _EVAL2 / slug / "prompt.gdpval.txt"
        assert gold_prompt.is_file(), gold_prompt
        assert exp_prompt.is_file(), exp_prompt
        assert gold_prompt.read_bytes() == exp_prompt.read_bytes(), slug
        for ready_id in _READY_GOLD_IDS:
            assert f"docs/eval/gdpval/{ready_id}/" not in source, (slug, ready_id)


def test_stub_prompts_are_todo_and_avoid_product_internals() -> None:
    for slug in _STUBS:
        text = (_EVAL2 / slug / "prompt.writeragent.txt").read_text(encoding="utf-8")
        assert text.lstrip().startswith("TODO"), slug
        lowered = text.lower()
        for banned in _BANNED_PRODUCT_INTERNALS:
            assert banned not in lowered, (slug, banned)
        for banned in _BANNED_STEERING:
            assert banned not in lowered, (slug, banned)


def test_writer_headed_template_is_parked() -> None:
    notes = (_EVAL2 / "writer-headed-template" / "notes.md").read_text(encoding="utf-8")
    source = (_EVAL2 / "writer-headed-template" / "SOURCE.md").read_text(encoding="utf-8")
    run = (_EVAL2 / "writer-headed-template" / "run.md").read_text(encoding="utf-8")
    assert "PARKED" in notes
    assert "PARKED" in source
    assert "#634" in notes
    assert _GOLD_STUBS["writer-headed-template"] in source
    assert "Needs gold materials" not in source
    assert "Do not run a headed trial" in run or "Do not run" in run


def test_stub_run_md_does_not_invent_helper_flags() -> None:
    for slug in _STUBS:
        run = (_EVAL2 / slug / "run.md").read_text(encoding="utf-8")
        assert "Not wired" in run or "not wired" in run, slug
        assert f"--task {slug}" not in run, slug
        assert "openai/gpt-oss-120b:nitro" in run
        assert "make deploy" in run


def test_user_facing_stub_notes_omit_duckdb() -> None:
    """Keep DuckDB off user-facing eval-2 stub notes."""
    for slug in _STUBS:
        for name in (
            "notes.md",
            "SOURCE.md",
            "run.md",
            "prompt.writeragent.txt",
            "rubric.eval2.md",
        ):
            text = (_EVAL2 / slug / name).read_text(encoding="utf-8")
            assert "duckdb" not in text.lower(), (slug, name)
