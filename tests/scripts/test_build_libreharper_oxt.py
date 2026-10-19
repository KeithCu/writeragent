# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LibreHarper OXT packaging, allowlist closure, and bundle verification."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from plugin.version import EXTENSION_VERSION
from plugin.writer.locale.harper_proofreader import (
    HARPER_LOCALE_TAGS,
    IMPLEMENTATION_NAME,
)
from scripts.build_libreharper_oxt import (
    FORBIDDEN_LIBREHARPER_ARCHIVE_PATHS,
    LIBREHARPER_BUNDLE_DIR,
    LIBREHARPER_EXTENSION_ID,
    REQUIRED_LIBREHARPER_ARCHIVE_PATHS,
    LibreHarperBuildError,
    assemble_libreharper_bundle,
    zip_libreharper_bundle,
)
from scripts.libreharper_bundle_paths import (
    LIBREHARPER_CLIENT_INIT,
    LIBREHARPER_DOC_INIT,
    LIBREHARPER_FORBIDDEN_CHAT_UI_MARKERS,
    LIBREHARPER_PLUGIN_FILES,
    LIBREHARPER_WRITER_INIT,
    collect_libreharper_plugin_paths,
    is_libreharper_forbidden_chat_ui,
)
from scripts.plugin_import_graph import (
    missing_dual_install_imports,
    missing_module_level_plugin_imports,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OOR = "http://openoffice.org/2001/registry"


def _n(local: str) -> str:
    return f"{{{_OOR}}}{local}"


def _local_tag(elem: ET.Element) -> str:
    t = elem.tag
    if t.startswith("{"):
        return t.rsplit("}", 1)[-1]
    return t


def _oor_name(elem: ET.Element) -> str | None:
    return elem.get(_n("name"))


def _child_node(parent: ET.Element, name: str) -> ET.Element:
    for c in parent:
        if _local_tag(c) == "node" and _oor_name(c) == name:
            return c
    raise AssertionError(f"missing <node oor:name={name!r}>")


def _setup_temp_project(tmp_path: Path) -> Path:
    """Create a minimal project tree suitable for assemble_libreharper_bundle."""
    base = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "extension-harper", base / "extension-harper")

    # Only logo.jpg is needed from assets
    logo_dst = base / "extension" / "assets" / "logo.jpg"
    logo_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "extension" / "assets" / "logo.jpg", logo_dst)

    # Minimal vendor directory with only json_repair
    v_dst = base / "vendor" / "json_repair" / "__init__.py"
    v_dst.parent.mkdir(parents=True, exist_ok=True)
    v_dst.write_text('"""json_repair stub."""\n', encoding="utf-8")

    # Copy allowlisted plugin files
    for rel in collect_libreharper_plugin_paths(str(PROJECT_ROOT)):
        dst = base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / rel, dst)

    # Shipped chatbot init needed for dual-install import scan
    cb_init = base / "plugin" / "chatbot" / "__init__.py"
    cb_init.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "plugin" / "chatbot" / "__init__.py", cb_init)

    return base


def test_linguistic_libreharper_grammar_xcu_locales_match_tags() -> None:
    path = os.path.join(
        PROJECT_ROOT,
        "extension-harper",
        "registry",
        "org",
        "openoffice",
        "Office",
        "LinguisticLibreHarperGrammar.xcu",
    )
    root = ET.parse(path).getroot()
    sm = _child_node(root, "ServiceManager")
    gc = _child_node(sm, "GrammarCheckers")
    impl = _child_node(gc, IMPLEMENTATION_NAME)
    locales_prop = None
    for c in impl:
        if _local_tag(c) == "prop" and _oor_name(c) == "Locales":
            locales_prop = c
            break
    assert locales_prop is not None
    val_el = next(c for c in locales_prop if _local_tag(c) == "value")
    values = [x.strip() for x in (val_el.text or "").split() if x.strip()]
    assert set(values) == set(HARPER_LOCALE_TAGS)


def test_libreharper_manifest_registers_harper_proofreader_only() -> None:
    path = os.path.join(PROJECT_ROOT, "extension-harper", "META-INF", "manifest.xml")
    body = open(path, encoding="utf-8").read()
    assert "plugin/writer/locale/harper_proofreader.py" in body
    assert "LinguisticLibreHarperGrammar.xcu" in body
    assert "ai_grammar_proofreader.py" not in body
    assert "CalcAddIns" not in body
    assert "Jobs.xcu" not in body

    root = ET.parse(path).getroot()
    full_paths: list[str] = []
    for el in root.iter():
        full = el.get("{http://openoffice.org/2001/manifest}full-path") or el.get("manifest:full-path")
        if full:
            full_paths.append(full)
    joined = "\n".join(full_paths)
    assert "ChatPanelFactory" not in joined
    assert "WriterAgentDeck" not in joined
    assert "ChatPanelDialog" not in joined
    assert "Factories.xcu" not in joined
    assert "Sidebar.xcu" not in joined
    assert "panel_factory.py" not in joined


def test_libreharper_bundle_excludes_chatpanel_ui() -> None:
    paths = collect_libreharper_plugin_paths(str(PROJECT_ROOT))
    leaked = [p for p in paths if is_libreharper_forbidden_chat_ui(p)]
    assert leaked == []
    assert "plugin/chatbot/panel_factory.py" in LIBREHARPER_FORBIDDEN_CHAT_UI_MARKERS
    assert is_libreharper_forbidden_chat_ui("registry/org/openoffice/Office/UI/Factories.xcu")
    assert is_libreharper_forbidden_chat_ui("Dialogs/ChatPanelDialog.xdl")
    assert is_libreharper_forbidden_chat_ui("plugin/chatbot/panel_factory.py")
    assert not is_libreharper_forbidden_chat_ui("plugin/writer/locale/harper_proofreader.py")

    harper_registry = os.path.join(
        PROJECT_ROOT, "extension-harper", "registry", "org", "openoffice", "Office"
    )
    assert os.path.isfile(os.path.join(harper_registry, "LinguisticLibreHarperGrammar.xcu"))
    assert not os.path.isdir(os.path.join(harper_registry, "UI"))


def test_collect_libreharper_plugin_paths() -> None:
    paths = collect_libreharper_plugin_paths(str(PROJECT_ROOT))
    assert "plugin/framework/config_schema.py" in paths
    assert "plugin/framework/client/errors.py" in paths
    assert "plugin/writer/locale/harper.py" in paths
    assert not any(p.endswith("harper_host.py") for p in paths)
    assert "plugin/doc/udprops.py" in paths
    assert "plugin/writer/locale/grammar_worker.py" in paths
    assert not any("llm_client" in p for p in paths)
    assert "plugin/framework/deal_shim.py" in paths
    assert "plugin/chatbot/extension_update_check.py" in paths
    assert "plugin/chatbot/dialogs.py" in paths
    assert "plugin/framework/client/requests.py" in paths
    assert "plugin/framework/client/http_transport.py" in paths
    assert "plugin/framework/module_base.py" in paths
    assert "plugin/chatbot/__init__.py" not in paths
    assert "plugin/framework/service.py" not in paths
    assert "plugin/framework/service.py" not in LIBREHARPER_PLUGIN_FILES


def test_libreharper_bundle_covers_top_level_plugin_imports() -> None:
    paths = collect_libreharper_plugin_paths(str(PROJECT_ROOT))
    missing = missing_module_level_plugin_imports(PROJECT_ROOT, paths)
    assert missing == []


def test_libreharper_bundle_covers_chatbot_package_init_for_dual_install() -> None:
    paths = collect_libreharper_plugin_paths(str(PROJECT_ROOT))
    missing = missing_dual_install_imports(PROJECT_ROOT, paths)
    assert missing == []


def test_libreharper_dual_install_can_import_extension_update_check(tmp_path: Path) -> None:
    root = PROJECT_ROOT
    stage = tmp_path / "bundle"
    for rel in collect_libreharper_plugin_paths(str(root)):
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
    cb_init = stage / "plugin" / "chatbot" / "__init__.py"
    cb_init.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "plugin" / "chatbot" / "__init__.py", cb_init)

    code = (
        "from plugin.chatbot.extension_update_check import schedule_extension_update_check_once\n"
        "assert schedule_extension_update_check_once is not None\n"
        "from plugin.framework.module_base import ModuleBase\n"
        "assert ModuleBase is not None\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(stage),
        env={**os.environ, "PYTHONPATH": str(stage), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_assemble_and_zip_libreharper_bundle(tmp_path: Path) -> None:
    base = _setup_temp_project(tmp_path)
    out_zip = base / "build" / "LibreHarper.oxt"

    # Assemble bundle and verify no update-libreharper.xml written to project root
    assembled_count = assemble_libreharper_bundle(str(base), strip=False)
    assert assembled_count > 0
    assert not (base / "update-libreharper.xml").exists()

    # Zip bundle
    rc = zip_libreharper_bundle(str(base), "build/LibreHarper.oxt")
    assert rc == 0
    assert out_zip.is_file()

    with zipfile.ZipFile(out_zip, "r") as zf:
        namelist = set(zf.namelist())

        for req in REQUIRED_LIBREHARPER_ARCHIVE_PATHS:
            assert req in namelist, f"missing required path in zip: {req}"

        for forbidden in FORBIDDEN_LIBREHARPER_ARCHIVE_PATHS:
            assert forbidden not in namelist, f"forbidden path in zip: {forbidden}"

        assert "plugin/framework/service.py" not in namelist
        assert "assets/logo.jpg" in namelist
        assert "assets/icon_64.png" not in namelist

        # Verify stub init contents
        assert zf.read("plugin/writer/__init__.py").decode("utf-8") == LIBREHARPER_WRITER_INIT
        assert zf.read("plugin/doc/__init__.py").decode("utf-8") == LIBREHARPER_DOC_INIT
        assert zf.read("plugin/framework/client/__init__.py").decode("utf-8") == LIBREHARPER_CLIENT_INIT

        # Verify manifest.xml full-paths all exist in zip
        m_tree = ET.fromstring(zf.read("META-INF/manifest.xml"))
        for elem in m_tree.findall(".//{http://openoffice.org/2001/manifest}file-entry"):
            fp = elem.attrib.get("{http://openoffice.org/2001/manifest}full-path")
            if fp and fp != "/":
                assert fp in namelist, f"manifest entry {fp} missing from zip"

        # Verify description.xml id and version
        d_tree = ET.fromstring(zf.read("description.xml"))
        id_val = None
        ver_val = None
        for el in d_tree.iter():
            tag = el.tag.split("}")[-1]
            if tag == "identifier":
                id_val = el.attrib.get("value")
            elif tag == "version":
                ver_val = el.attrib.get("value")
        assert id_val == LIBREHARPER_EXTENSION_ID
        assert ver_val == EXTENSION_VERSION

        # Verify plugin/_manifest.py has doc module with default harper
        manifest_ns: dict[str, object] = {}
        exec(zf.read("plugin/_manifest.py").decode("utf-8"), manifest_ns)
        modules = manifest_ns.get("MODULES", [])
        assert isinstance(modules, list)
        doc_mod = next(m for m in modules if isinstance(m, dict) and m.get("name") == "doc")
        cfg = doc_mod.get("config", {})
        assert cfg.get("grammar_proofreader_enabled", {}).get("default") == "harper"


def test_assemble_fails_when_vendor_missing(tmp_path: Path) -> None:
    base = _setup_temp_project(tmp_path)
    out_zip = base / "build" / "LibreHarper.oxt"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(b"sentinel-zip-data")

    # Remove vendor/ directory
    shutil.rmtree(base / "vendor")

    with pytest.raises(LibreHarperBuildError):
        assemble_libreharper_bundle(str(base), strip=False)

    # Pre-existing zip must remain untouched
    assert out_zip.read_bytes() == b"sentinel-zip-data"


def test_assemble_fails_when_description_tpl_missing(tmp_path: Path) -> None:
    base = _setup_temp_project(tmp_path)
    out_zip = base / "build" / "LibreHarper.oxt"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(b"sentinel-zip-data")

    # Remove description.xml.tpl
    (base / "extension-harper" / "description.xml.tpl").unlink()

    with pytest.raises(LibreHarperBuildError):
        assemble_libreharper_bundle(str(base), strip=False)

    # Pre-existing zip must remain untouched
    assert out_zip.read_bytes() == b"sentinel-zip-data"


def test_zip_fails_when_description_xml_missing(tmp_path: Path) -> None:
    base = _setup_temp_project(tmp_path)
    assemble_libreharper_bundle(str(base), strip=False)

    out_zip = base / "build" / "LibreHarper.oxt"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(b"sentinel-zip-data")

    # Remove description.xml from bundle
    bundle_desc = base / LIBREHARPER_BUNDLE_DIR / "description.xml"
    bundle_desc.unlink()

    with pytest.raises(LibreHarperBuildError):
        zip_libreharper_bundle(str(base), "build/LibreHarper.oxt")

    # Sentinel output zip must be unchanged
    assert out_zip.read_bytes() == b"sentinel-zip-data"
