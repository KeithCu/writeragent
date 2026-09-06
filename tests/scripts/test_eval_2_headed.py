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
    EVAL_2_MAX_TOOL_ROUNDS,
    MAX_TOOL_ROUNDS_KEY,
    find_writeragent_json,
    temporary_max_tool_rounds,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_temporary_max_tool_rounds_restores_missing_key(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    original = '{\n    "text_model": "keep-me"\n}\n'
    _write(config, original)
    with temporary_max_tool_rounds(config):
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data[MAX_TOOL_ROUNDS_KEY] == EVAL_2_MAX_TOOL_ROUNDS
        assert data["text_model"] == "keep-me"
    assert config.read_text(encoding="utf-8") == original


def test_temporary_max_tool_rounds_restores_prior_value(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    original = json.dumps({MAX_TOOL_ROUNDS_KEY: 20, "endpoint": "http://x"}, indent=4) + "\n"
    _write(config, original)
    with temporary_max_tool_rounds(config):
        assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 50
    assert config.read_text(encoding="utf-8") == original


def test_temporary_max_tool_rounds_restores_after_error(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    original = '{"ok": true}\n'
    _write(config, original)
    with pytest.raises(RuntimeError, match="boom"):
        with temporary_max_tool_rounds(config):
            raise RuntimeError("boom")
    assert config.read_text(encoding="utf-8") == original


def test_temporary_max_tool_rounds_strips_comment_header(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    original = "// schema\n{\n    \"text_model\": \"gpt\"\n}\n"
    _write(config, original)
    with temporary_max_tool_rounds(config):
        assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 50
    assert config.read_text(encoding="utf-8") == original


def test_find_writeragent_json_explicit(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    _write(path, "{}")
    assert find_writeragent_json(path) == path


def test_find_writeragent_json_missing() -> None:
    with pytest.raises(FileNotFoundError, match="writeragent.json"):
        find_writeragent_json(candidates=[])
