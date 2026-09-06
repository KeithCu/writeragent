# WriterAgent tests for scripts/eval_2_headed.py
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_headed import (  # noqa: E402
    DEFAULT_MAX_TOOL_ROUNDS,
    EVAL_2_MAX_TOOL_ROUNDS,
    MAX_TOOL_ROUNDS_KEY,
    apply_max_tool_rounds,
    find_writeragent_json,
    read_max_tool_rounds,
    restore_max_tool_rounds,
    temporary_max_tool_rounds,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_read_max_tool_rounds_absent_is_15() -> None:
    assert read_max_tool_rounds({}) == DEFAULT_MAX_TOOL_ROUNDS
    assert DEFAULT_MAX_TOOL_ROUNDS == 15


def test_apply_and_restore_removes_absent_key() -> None:
    data: dict[str, object] = {"text_model": "keep"}
    previous = apply_max_tool_rounds(data, 50)
    assert previous is None
    assert data[MAX_TOOL_ROUNDS_KEY] == 50
    restore_max_tool_rounds(data, previous)
    assert MAX_TOOL_ROUNDS_KEY not in data
    assert data["text_model"] == "keep"


def test_apply_and_restore_keeps_prior_value() -> None:
    data: dict[str, object] = {MAX_TOOL_ROUNDS_KEY: 20}
    previous = apply_max_tool_rounds(data, EVAL_2_MAX_TOOL_ROUNDS)
    assert previous == 20
    assert data[MAX_TOOL_ROUNDS_KEY] == 50
    restore_max_tool_rounds(data, previous)
    assert data[MAX_TOOL_ROUNDS_KEY] == 20


def test_temporary_max_tool_rounds_removes_key_when_absent(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, '{\n    "text_model": "keep-me"\n}\n')
    with temporary_max_tool_rounds(config):
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data[MAX_TOOL_ROUNDS_KEY] == EVAL_2_MAX_TOOL_ROUNDS
        assert data["text_model"] == "keep-me"
    restored = json.loads(config.read_text(encoding="utf-8"))
    assert MAX_TOOL_ROUNDS_KEY not in restored
    assert restored["text_model"] == "keep-me"


def test_temporary_max_tool_rounds_restores_prior_value(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, json.dumps({MAX_TOOL_ROUNDS_KEY: 20, "endpoint": "http://x"}, indent=4) + "\n")
    with temporary_max_tool_rounds(config):
        assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 50
    assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 20


def test_temporary_max_tool_rounds_restores_after_error(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, '{"ok": true}\n')
    with pytest.raises(RuntimeError, match="boom"):
        with temporary_max_tool_rounds(config):
            raise RuntimeError("boom")
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(config.read_text(encoding="utf-8"))


def test_temporary_max_tool_rounds_keeps_comment_header(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, "// schema\n{\n    \"text_model\": \"gpt\"\n}\n")
    with temporary_max_tool_rounds(config):
        text = config.read_text(encoding="utf-8")
        assert text.startswith("// schema")
        assert json.loads(text.split("\n", 1)[1])[MAX_TOOL_ROUNDS_KEY] == 50
    restored = config.read_text(encoding="utf-8")
    assert restored.startswith("// schema")
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(restored.split("\n", 1)[1])


def test_find_writeragent_json_explicit(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    _write(path, "{}")
    assert find_writeragent_json(path) == path


def test_find_writeragent_json_missing() -> None:
    with pytest.raises(FileNotFoundError, match="writeragent.json"):
        find_writeragent_json(candidates=[])
