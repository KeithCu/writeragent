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
    preflight_writeragent,
    remap_path,
    resolve_archive_sources,
    resolve_bundle_path,
    resolve_output_path,
    should_exclude,
    strip_debug_menu_xml,
    sync_vendor_into_lib,
    verify_bundle_manifest,
    write_oxt_zip,
)
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param("extension/assets/python_logo.svg", "extension/assets/python_logo.NOTICE", "extension/assets/python_32.png", id="test_python_logo_dev_sources_excluded_from_oxt"),
        pytest.param("extension/assets/jupyter_logo.svg", "extension/assets/jupyter_logo.NOTICE", "extension/assets/gear_32.png", id="test_jupyter_logo_dev_sources_excluded_from_oxt"),
    ],
)
def test_python_logo_dev_sources_excluded_from_oxt(value, value_2, value_3):
    assert should_exclude(value) is True
    assert should_exclude(value_2) is True
    assert should_exclude(value_3) is False

def test_provider_logo_pngs_ship_notice_excluded():
    assert should_exclude("extension/assets/provider_logos.NOTICE") is True
    for stem in ("openrouter", "together", "huggingface", "nvidia"):
        for px in (16, 32, 48):
            assert should_exclude("extension/assets/%s_%s.png" % (stem, px)) is False
    assert remap_path("extension/assets/openrouter_48.png") == "assets/openrouter_48.png"
    assert remap_path("extension/assets/openrouter_16.png") == "assets/openrouter_16.png"


def test_sidebar_test_hooks_excluded_from_release_oxt():
    """Release ``--no-tests`` must not ship mock-LLM sidebar drivers."""
    path = "plugin/chatbot/sidebar_test_hooks.py"
    assert should_exclude(path, with_tests=False) is True
    assert should_exclude(path, with_tests=True) is False


def test_pyspector_cache_excluded_from_oxt():
    """Hot-deploy and OXT must not ship make pyspector AST cache under plugin/."""
    assert should_exclude("plugin/.pyspector_cache") is True
    assert should_exclude("plugin/.pyspector_cache/ast/foo.json") is True
    assert should_exclude("plugin/.pyspector_baseline.json") is True
    assert should_exclude("plugin/main.py") is False


def test_should_exclude_component_matching():
    # Path components matching .git, .DS_Store, __pycache__, .pyspector_cache
    assert should_exclude(".git") is True
    assert should_exclude("foo/.git/HEAD") is True
    assert should_exclude(".DS_Store") is True
    assert should_exclude("plugin/__pycache__/foo.cpython-313.pyc") is True
    assert should_exclude("plugin/.pyspector_cache/data.json") is True

    # Basenames matching .pyspector_baseline.json, .pyc, .pyo, .tpl
    assert should_exclude("plugin/.pyspector_baseline.json") is True
    assert should_exclude("extension/description.xml.tpl") is True
    assert should_exclude("plugin/main.pyc") is True
    assert should_exclude("plugin/main.pyo") is True

    # Tests directory and test_ components
    assert should_exclude("plugin/tests/test_foo.py", with_tests=False) is True
    assert should_exclude("plugin/tests/test_foo.py", with_tests=True) is False
    assert should_exclude("plugin/foo/test_foo.py", with_tests=False) is True
    assert should_exclude("plugin/foo/test_foo.py", with_tests=True) is True
    assert should_exclude("plugin/foo/tests/helper.py", with_tests=False) is True
    assert should_exclude("plugin/foo/tests/helper.py", with_tests=True) is True

    # Retained files that substring 'test_' or '.git' previously dropped
    assert should_exclude("contest_entry.py") is False
    assert should_exclude("latest_thing.py") is False
    assert should_exclude(".github/workflows/ci.yml") is False
    assert should_exclude(".gitignore") is False


def test_generated_includes_single_dialogs_tree():
    """Lowercase dialogs/ must not be packaged — it collides with Dialogs/ on Windows."""
    assert "build/generated/Dialogs/" in GENERATED_INCLUDES
    assert "build/generated/dialogs/" not in GENERATED_INCLUDES
    assert "build/generated/Addons.xcu" not in GENERATED_INCLUDES
    assert "build/generated/Accelerators.xcu" not in GENERATED_INCLUDES
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
    (vendor / "bin").mkdir()
    (vendor / "bin" / "l2m").write_text("#!/bin/sh\n", encoding="utf-8")
    lib = tmp_path / "plugin" / "lib"
    (lib / "stale_pkg").mkdir(parents=True)
    (lib / "stale_pkg" / "old.py").write_text("old\n", encoding="utf-8")

    n = sync_vendor_into_lib(str(vendor), str(lib), prune_websockets=False)
    assert n == 1
    assert (lib / "isodate" / "__init__.py").is_file()
    assert not (lib / "isodate-0.7.2.dist-info").exists()
    assert not (lib / ".cache").exists()
    assert not (lib / "bin").exists()
    assert not (lib / "stale_pkg").exists()

    # Replace existing tree on re-sync
    (lib / "isodate" / "stale.py").write_text("stale\n", encoding="utf-8")
    sync_vendor_into_lib(str(vendor), str(lib), prune_websockets=False)
    assert not (lib / "isodate" / "stale.py").exists()


def test_sync_vendor_into_lib_keeps_only_dist_info_metadata(tmp_path):
    """latex2mathml calls importlib.metadata.version at import; METADATA must ship."""
    vendor = tmp_path / "vendor"
    (vendor / "latex2mathml").mkdir(parents=True)
    (vendor / "latex2mathml" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    dist = vendor / "latex2mathml-3.81.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: latex2mathml\nVersion: 3.81.1\n", encoding="utf-8")
    (dist / "RECORD").write_text("", encoding="utf-8")
    lib = tmp_path / "plugin" / "lib"
    assert sync_vendor_into_lib(str(vendor), str(lib), prune_websockets=False) == 1
    assert (lib / "latex2mathml-3.81.1.dist-info" / "METADATA").is_file()
    assert not (lib / "latex2mathml-3.81.1.dist-info" / "RECORD").exists()


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
    assert not (src / "plugin" / "lib").exists()
    assert not (dest / "plugin" / "lib").exists()


def test_skip_zip_does_not_call_zip_bundle(monkeypatch, tmp_path):
    assembled: dict[str, object] = {}

    def fake_assemble(base_dir, modules=None, **kwargs):
        assembled["bundle_dir"] = kwargs.get("bundle_dir")
        return 1

    zipped: list[str] = []

    def fake_zip(base_dir, output, bundle_dir=None):
        zipped.append(output)
        return 0

    monkeypatch.setattr("scripts.build_oxt.preflight_writeragent", lambda base_dir: None)
    monkeypatch.setattr("scripts.build_oxt.verify_bundle_manifest", lambda *a, **k: None)
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


def test_strip_debug_menu_xml_on_real_addons_xcu():
    import xml.etree.ElementTree as ET
    addons = os.path.join(PROJECT_ROOT, "extension", "Addons.xcu")
    with open(addons, encoding="utf-8") as handle:
        original = handle.read()
    assert DEBUG_MENU_NODE_MARKER in original
    stripped = strip_debug_menu_xml(original)
    assert DEBUG_MENU_NODE_MARKER not in stripped
    assert 'oor:name="M16"' in stripped
    assert 'oor:name="M18"' in stripped
    assert "OfficeToolBar" in stripped
    root = ET.fromstring(stripped)
    assert root is not None


def test_preflight_writeragent_missing_inputs(tmp_path):
    with pytest.raises(RuntimeError) as exc_info:
        preflight_writeragent(str(tmp_path))
    assert "make manifest" in str(exc_info.value)


def test_main_preflight_fails_and_names_make_target(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("scripts.build_oxt.PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["build_oxt.py"])
    rc = main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "make manifest" in err


def test_verify_bundle_manifest_missing_file(tmp_path):
    meta_inf = tmp_path / "META-INF"
    meta_inf.mkdir(parents=True)
    manifest = meta_inf / "manifest.xml"
    manifest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">\n'
        '  <manifest:file-entry manifest:media-type="application/vnd.sun.star.configuration-data" manifest:full-path="nonexistent.xcu" />\n'
        '</manifest:manifest>\n',
        encoding="utf-8",
    )
    (tmp_path / "plugin").mkdir(parents=True)
    (tmp_path / "plugin" / "_manifest.py").write_text("#\n", encoding="utf-8")
    (tmp_path / "Addons.xcu").write_text("<xml/>\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        verify_bundle_manifest(str(tmp_path))
    assert "nonexistent.xcu" in str(exc_info.value)


def test_resolve_archive_sources_collisions_and_collapse(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f3 = tmp_path / "c.txt"
    f1.write_text("same", encoding="utf-8")
    f2.write_text("same", encoding="utf-8")
    f3.write_text("different", encoding="utf-8")

    # Identical collapse
    sources = resolve_archive_sources(str(tmp_path), ["a.txt", "b.txt"], remap_fn=lambda _: "target.txt")
    assert len(sources) == 1

    # Differing doubles fail
    with pytest.raises(SystemExit):
        resolve_archive_sources(str(tmp_path), ["a.txt", "c.txt"], remap_fn=lambda _: "target.txt")


def test_write_oxt_zip_deterministic_and_atomic(tmp_path, monkeypatch):
    import zipfile
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "b.txt").write_text("hello b", encoding="utf-8")
    (bundle / "a.txt").write_text("hello a", encoding="utf-8")

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    out1 = tmp_path / "out1.oxt"
    out2 = tmp_path / "out2.oxt"

    count1 = write_oxt_zip(str(bundle), str(out1))
    count2 = write_oxt_zip(str(bundle), str(out2))
    assert count1 == 2
    assert count2 == 2

    with zipfile.ZipFile(out1) as z1, zipfile.ZipFile(out2) as z2:
        assert z1.namelist() == ["a.txt", "b.txt"]
        assert z1.namelist() == z2.namelist()
        for name in z1.namelist():
            assert z1.getinfo(name).date_time == z2.getinfo(name).date_time

    # Atomic write leaves existing output intact on error
    prev_bytes = out1.read_bytes()

    def fail_writestr(*args, **kwargs):
        raise OSError("disk full simulated")

    monkeypatch.setattr("zipfile.ZipFile.writestr", fail_writestr)
    with pytest.raises(OSError):
        write_oxt_zip(str(bundle), str(out1))
    assert out1.read_bytes() == prev_bytes


def test_verify_bundle_manifest_requires_audio_catalog(tmp_path):
    meta_inf = tmp_path / "META-INF"
    meta_inf.mkdir(parents=True)
    (meta_inf / "manifest.xml").write_text(
        '<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest"/>',
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "_manifest.py").write_text("", encoding="utf-8")
    (tmp_path / "Addons.xcu").write_text("", encoding="utf-8")
    (plugin_dir / "audio").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="voice_catalog.json"):
        verify_bundle_manifest(str(tmp_path))


def test_verify_bundle_manifest_accepts_audio_catalog(tmp_path):
    meta_inf = tmp_path / "META-INF"
    meta_inf.mkdir(parents=True)
    (meta_inf / "manifest.xml").write_text(
        '<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest"/>',
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "_manifest.py").write_text("", encoding="utf-8")
    (tmp_path / "Addons.xcu").write_text("", encoding="utf-8")
    audio_data = plugin_dir / "audio" / "data"
    audio_data.mkdir(parents=True)
    (audio_data / "voice_catalog.json").write_text("{}", encoding="utf-8")

    verify_bundle_manifest(str(tmp_path))


def test_audio_voice_catalog_is_included():
    from scripts.build_oxt import get_always_include_plugin

    assert os.path.isfile(os.path.join(PROJECT_ROOT, "plugin/audio/data/voice_catalog.json"))
    assert should_exclude("plugin/audio/data/voice_catalog.json") is False
    assert "plugin/audio/" in get_always_include_plugin(PROJECT_ROOT)


