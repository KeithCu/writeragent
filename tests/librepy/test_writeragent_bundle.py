"""LibrePy bundle includes writeragent namespace stub, not full tool API."""

from __future__ import annotations

import os

from scripts.librepy_bundle_paths import collect_librepy_plugin_paths


def test_librepy_bundle_includes_writeragent_namespace():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = collect_librepy_plugin_paths(repo_root)
    assert "plugin/scripting/writeragent_namespace.py" in paths


def test_librepy_bundle_excludes_writeragent_api():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = collect_librepy_plugin_paths(repo_root)
    assert "plugin/scripting/writeragent_api.py" not in paths


def test_librepy_bundle_excludes_prompts_and_chat_calc_modules():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = collect_librepy_plugin_paths(repo_root)
    assert "plugin/framework/prompts.py" not in paths
    assert "plugin/calc/base.py" not in paths
    assert "plugin/calc/python/venv.py" not in paths
    assert "plugin/calc/calc_addin_data.py" in paths
