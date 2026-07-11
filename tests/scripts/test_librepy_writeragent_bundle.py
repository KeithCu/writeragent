"""LibrePy bundle includes writeragent namespace stub, not full tool API."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import collect_librepy_plugin_paths  # noqa: E402


def test_librepy_bundle_includes_writeragent_namespace():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/scripting/writeragent_namespace.py" in paths


def test_librepy_bundle_excludes_writeragent_api():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/scripting/writeragent_api.py" not in paths


def test_librepy_bundle_excludes_prompts_and_chat_calc_modules():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/framework/prompts.py" not in paths
    assert "plugin/calc/base.py" not in paths
    assert "plugin/calc/python/venv.py" not in paths
    assert "plugin/calc/calc_addin_data.py" in paths
