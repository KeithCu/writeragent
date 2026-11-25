# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for tests.harness.uno_profile_config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.framework.config import CONFIG_SCHEMA_COMMENT, parse_config_json_text
from plugin.testing_runner import current_office
from tests.harness import uno_profile_config


@pytest.fixture
def throwaway_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the runner's throwaway profile at *tmp_path* and stub soffice sleep/venv."""
    monkeypatch.setattr(current_office(), "throwaway_profile_dirs", [tmp_path])
    monkeypatch.setattr(uno_profile_config, "settle_soffice_config", lambda: None)
    monkeypatch.setattr(uno_profile_config, "project_venv", lambda: "/repo/.venv")
    return tmp_path / "user" / "config" / "writeragent.json"


def test_repo_venv_survives_schema_header_and_restores(throwaway_cfg: Path) -> None:
    throwaway_cfg.parent.mkdir(parents=True, exist_ok=True)
    original = {"a": 1, "scripting.python_venv_path": "/old"}
    throwaway_cfg.write_text(CONFIG_SCHEMA_COMMENT + json.dumps(original) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        with uno_profile_config.repo_venv_in_profile():
            inside = parse_config_json_text(throwaway_cfg.read_text(encoding="utf-8"))
            assert inside == {"a": 1, "scripting.python_venv_path": "/repo/.venv"}
            raise RuntimeError("boom")

    restored = parse_config_json_text(throwaway_cfg.read_text(encoding="utf-8"))
    assert restored == original


def test_repo_venv_removes_key_that_was_not_set(throwaway_cfg: Path) -> None:
    throwaway_cfg.parent.mkdir(parents=True, exist_ok=True)
    throwaway_cfg.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")

    with uno_profile_config.repo_venv_in_profile():
        inside = parse_config_json_text(throwaway_cfg.read_text(encoding="utf-8"))
        assert inside == {"a": 1, "scripting.python_venv_path": "/repo/.venv"}

    restored = parse_config_json_text(throwaway_cfg.read_text(encoding="utf-8"))
    assert restored == {"a": 1}


def test_patch_rejects_unreadable_config(throwaway_cfg: Path) -> None:
    throwaway_cfg.parent.mkdir(parents=True, exist_ok=True)
    throwaway_cfg.write_bytes(b"not json")

    with pytest.raises(AssertionError, match="unreadable test profile config"):
        uno_profile_config.patch_profile_config(lambda d: d.update({"x": 1}))

    assert throwaway_cfg.read_bytes() == b"not json"


def test_no_throwaway_profile_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(current_office(), "throwaway_profile_dirs", [])

    with pytest.raises(AssertionError, match="no throwaway test profile"):
        uno_profile_config.profile_config_path()


def test_profile_config_values_returns_only_the_named_keys(throwaway_cfg: Path) -> None:
    throwaway_cfg.parent.mkdir(parents=True, exist_ok=True)
    throwaway_cfg.write_text(
        CONFIG_SCHEMA_COMMENT
        + json.dumps(
            {
                "api_key": "secret-value",
                "scripting.python_session_mode": "shared",
                "other": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    got = uno_profile_config.profile_config_values(
        "scripting.python_session_mode", "scripting.python_venv_path"
    )
    assert got == {"scripting.python_session_mode": "shared"}
