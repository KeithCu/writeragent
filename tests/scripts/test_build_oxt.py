# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for OXT build path exclusions."""

from __future__ import annotations

import os

from scripts.build_oxt import (
    DEBUG_MENU_NODE_MARKER,
    GENERATED_INCLUDES,
    assemble_bundle,
    main,
    remap_path,
    resolve_bundle_path,
    resolve_output_path,
    should_exclude,
    sync_vendor_into_lib,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_python_logo_dev_sources_excluded_from_oxt():
    assert should_exclude("extension/assets/python_logo.svg") is True
    assert should_exclude("extension/assets/python_logo.NOTICE") is True
    assert should_exclude("extension/assets/python_32.png") is False


def test_jupyter_logo_dev_sources_excluded_from_oxt():
    assert should_exclude("extension/assets/jupyter_logo.svg") is True
    assert should_exclude("extension/assets/jupyter_logo.NOTICE") is True
    assert should_exclude("extension/assets/jupyter_32.png") is False


def test_provider_logo_pngs_ship_notice_excluded():
    assert should_exclude("extension/assets/provider_logos.NOTICE") is True
    for stem in ("openrouter", "together", "huggingface", "nvidia"):
        assert should_exclude("extension/assets/%s_48.png" % stem) is False
    assert remap_path("extension/assets/openrouter_48.png") == "assets/openrouter_48.png"


def test_pyspector_cache_excluded_from_oxt():
    """Hot-deploy and OXT must not ship make pyspector AST cache under plugin/."""
    assert should_exclude("plugin/.pyspector_cache") is True
    assert should_exclude("plugin/.pyspector_cache/ast/foo.json") is True
    assert should_exclude("plugin/.pyspector_baseline.json") is True
    assert should_exclude("plugin/main.py") is False


def test_generated_includes_single_dialogs_tree():
    """Lowercase dialogs/ must not be packaged — it collides with Dialogs/ on Windows."""
    assert "build/generated/Dialogs/" in GENERATED_INCLUDES
    assert "build/generated/dialogs/" not in GENERATED_INCLUDES
    lower = [p for p in GENERATED_INCLUDES if p.replace("\\", "/").rstrip("/").endswith("dialogs")]
    assert lower == [], "GENERATED_INCLUDES must not list a lowercase dialogs/ path: %s" % lower


def test_remap_path_chat_panel_and_generated_dialogs():
    assert remap_path("extension/Dialogs/ChatPanelDialog.xdl") == "Dialogs/ChatPanelDialog.xdl"
    assert remap_path("build/generated/Dialogs/SettingsDialog.xdl") == "Dialogs/SettingsDialog.xdl"
    assert remap_path("build/generated/Dialogs/chatbot.xdl") == "Dialogs/chatbot.xdl"


def test_extension_chat_panel_xdl_exists():
    path = os.path.join(PROJECT_ROOT, "extension", "Dialogs", "ChatPanelDialog.xdl")
    assert os.path.isfile(path), "sidebar XDL missing at %s" % path


def test_sync_vendor_into_lib_copies_isodate(tmp_path):
    """Hot-deploy needs isodate under plugin/lib or datetime_wire fails to import."""
    vendor = tmp_path / "vendor"
    (vendor / "isodate").mkdir(parents=True)
    (vendor / "isodate" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (vendor / "isodate-0.7.2.dist-info").mkdir()
    (vendor / ".cache").mkdir()
    lib = tmp_path / "plugin" / "lib"
    n = sync_vendor_into_lib(str(vendor), str(lib), prune_websockets=False)
    assert n == 1
    assert (lib / "isodate" / "__init__.py").is_file()
    assert not (lib / "isodate-0.7.2.dist-info").exists()
    assert not (lib / ".cache").exists()
    # Replace existing tree on re-sync
    (lib / "isodate" / "stale.py").write_text("stale\n", encoding="utf-8")
    sync_vendor_into_lib(str(vendor), str(lib), prune_websockets=False)
    assert not (lib / "isodate" / "stale.py").exists()


def test_resolve_bundle_path_absolute_and_relative():
    assert resolve_bundle_path("/proj", "build/bundle") == os.path.join("/proj", "build/bundle")
    assert resolve_bundle_path("/proj", "/tmp/writeragent-release-abc") == "/tmp/writeragent-release-abc"


def test_resolve_output_path_absolute_and_relative():
    assert resolve_output_path("/proj", "build/WriterAgent.oxt") == os.path.join("/proj", "build/WriterAgent.oxt")
    assert resolve_output_path("/proj", "/tmp/out.oxt") == "/tmp/out.oxt"


def test_assemble_bundle_writes_to_absolute_bundle_dir(tmp_path):
    """``--bundle-dir`` / assemble_bundle must land files in an absolute dest, not build/bundle."""
    src = tmp_path / "proj"
    plugin = src / "plugin"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("#\n", encoding="utf-8")
    (plugin / "main.py").write_text("x = 1\n", encoding="utf-8")
    dest = tmp_path / "bundle_out"
    dest.mkdir()
    assemble_bundle(str(src), modules=[], with_tests=True, strip=False, bundle_dir=str(dest))
    assert (dest / "plugin" / "main.py").is_file()
    assert (dest / "plugin" / "main.py").read_text(encoding="utf-8") == "x = 1\n"


def test_skip_zip_does_not_call_zip_bundle(monkeypatch, tmp_path):
    assembled: dict[str, object] = {}

    def fake_assemble(base_dir, modules, **kwargs):
        assembled["bundle_dir"] = kwargs.get("bundle_dir")
        return 1

    zipped: list[str] = []

    def fake_zip(base_dir, output, bundle_dir=None):
        zipped.append(output)
        return 0

    monkeypatch.setattr("scripts.build_oxt.assemble_bundle", fake_assemble)
    monkeypatch.setattr("scripts.build_oxt.zip_bundle", fake_zip)
    monkeypatch.setattr(
        "sys.argv",
        ["build_oxt.py", "--strip", "--bundle-dir", str(tmp_path), "--skip-zip"],
    )
    assert main() == 0
    assert assembled["bundle_dir"] == str(tmp_path)
    assert zipped == []


def test_debug_menu_strip_marker_matches_addons_xcu():
    """Release OXT strip must find the Debug parent node (not M16a children)."""
    addons = os.path.join(PROJECT_ROOT, "extension", "Addons.xcu")
    with open(addons, encoding="utf-8") as handle:
        text = handle.read()
    assert DEBUG_MENU_NODE_MARKER in text
    marker_at = text.find(DEBUG_MENU_NODE_MARKER)
    window = text[marker_at : marker_at + 400]
    assert "Debug" in window
    assert "M16a" not in DEBUG_MENU_NODE_MARKER
