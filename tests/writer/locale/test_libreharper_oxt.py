# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LibreHarper OXT packaging and Linguistic XCU."""

from __future__ import annotations

import ast
import os
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from plugin.writer.locale.harper_proofreader import HARPER_LOCALE_TAGS, IMPLEMENTATION_NAME, normalize_harper_locale_to_bcp47

_OOR = "http://openoffice.org/2001/registry"


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


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


def test_linguistic_libreharper_grammar_xcu_locales_match_tags() -> None:
    path = os.path.join(
        _repo_root(),
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
    assert val_el.text is not None
    assert tuple(val_el.text.split()) == HARPER_LOCALE_TAGS


def test_libreharper_preserves_supported_english_dialects() -> None:
    for country, expected in (("US", "en-US"), ("GB", "en-GB"), ("AU", "en-AU"), ("CA", "en-CA"), ("IN", "en-IN")):
        locale = SimpleNamespace(Language="en", Country=country, Variant="")
        assert normalize_harper_locale_to_bcp47(locale) == expected

    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="en-AU", Country="", Variant="")) == "en-AU"
    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="de", Country="DE", Variant="")) is None


def test_libreharper_manifest_registers_harper_proofreader_only() -> None:
    path = os.path.join(_repo_root(), "extension-harper", "META-INF", "manifest.xml")
    body = open(path, encoding="utf-8").read()
    assert "plugin/writer/locale/harper_proofreader.py" in body
    assert "LinguisticLibreHarperGrammar.xcu" in body
    assert "ai_grammar_proofreader.py" not in body
    assert "CalcAddIns" not in body
    assert "Jobs.xcu" not in body
    # E: WriterAgent owns the sidebar. Harper file-entries must not claim chat UI.
    root = ET.parse(path).getroot()
    full_paths = []
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
    from scripts.libreharper_bundle_paths import (
        LIBREHARPER_FORBIDDEN_CHAT_UI_MARKERS,
        collect_libreharper_plugin_paths,
        is_libreharper_forbidden_chat_ui,
    )

    paths = collect_libreharper_plugin_paths(_repo_root())
    leaked = [p for p in paths if is_libreharper_forbidden_chat_ui(p)]
    assert leaked == []
    assert "plugin/chatbot/panel_factory.py" in LIBREHARPER_FORBIDDEN_CHAT_UI_MARKERS
    assert is_libreharper_forbidden_chat_ui("registry/org/openoffice/Office/UI/Factories.xcu")
    assert is_libreharper_forbidden_chat_ui("Dialogs/ChatPanelDialog.xdl")
    assert is_libreharper_forbidden_chat_ui("plugin/chatbot/panel_factory.py")
    assert not is_libreharper_forbidden_chat_ui("plugin/writer/locale/harper_proofreader.py")
    # Harper copies extension-harper/registry (grammar XCU only) and assets, not UI.
    harper_registry = os.path.join(
        _repo_root(), "extension-harper", "registry", "org", "openoffice", "Office"
    )
    assert os.path.isfile(os.path.join(harper_registry, "LinguisticLibreHarperGrammar.xcu"))
    assert not os.path.isdir(os.path.join(harper_registry, "UI"))


def test_grammar_work_queue_has_no_top_level_framework_client_package_import() -> None:
    """Harper OXT must not pull LLM/embeddings via ``from plugin.framework.client import …``."""
    path = os.path.join(_repo_root(), "plugin", "writer", "locale", "grammar_work_queue.py")
    with open(path, encoding="utf-8") as f:
        module = ast.parse(f.read(), filename=path)
    top_level = [
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "plugin.framework.client" not in top_level
    assert "plugin.framework.client.request_controls" not in top_level
    assert "plugin.framework.client.llm_client" not in top_level
    assert "plugin.framework.client.model_fetcher" not in top_level


def test_libreharper_bundle_covers_top_level_plugin_imports() -> None:
    """Every ``plugin.*`` a shipped module imports at load time must also ship.

    ``unopkg add`` executes ``harper_proofreader.py``, so a module missing from
    the allowlist fails installation rather than degrading at runtime. Two were
    missing this way: ``config.py`` reaches ``config_schema`` via
    ``from plugin.framework import config_schema`` and ``client/requests.py``
    reaches ``client/errors.py`` via ``from .errors import ...`` — neither is a
    plain ``import plugin.x.y``, so both need submodule-aware resolution.
    """
    from pathlib import Path

    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths
    from tests.scripts.test_librepy_import_graph import (
        _is_shipped_plugin_module,
        _module_level_import_nodes,
        _plugin_mod_to_candidates,
        _resolved_import_from_module,
    )

    root = Path(_repo_root())
    shipped = set(collect_libreharper_plugin_paths(str(root)))
    missing: list[str] = []

    for rel in sorted(shipped):
        if not rel.endswith(".py"):
            continue
        path = root / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in _module_level_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("plugin.") and not _is_shipped_plugin_module(alias.name, shipped):
                        missing.append(f"{rel} -> import {alias.name}")
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = _resolved_import_from_module(path, node)
            if not mod or not (mod == "plugin" or mod.startswith("plugin.")):
                continue
            if not _is_shipped_plugin_module(mod, shipped):
                missing.append(f"{rel} -> from {mod}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub = f"{mod}.{alias.name}"
                if not any((root / cand).is_file() for cand in _plugin_mod_to_candidates(sub)):
                    continue  # an attribute, not a submodule
                if not _is_shipped_plugin_module(sub, shipped):
                    missing.append(f"{rel} -> from {mod} import {alias.name}")

    assert missing == []


def test_collect_libreharper_plugin_paths() -> None:
    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths

    paths = collect_libreharper_plugin_paths(_repo_root())
    assert "plugin/framework/config_schema.py" in paths
    assert "plugin/framework/client/errors.py" in paths
    assert "plugin/writer/locale/harper.py" in paths
    assert not any(p.endswith("harper_host.py") for p in paths)
    assert "plugin/doc/udprops.py" in paths
    assert "plugin/writer/locale/grammar_worker.py" in paths
    assert not any("llm_client" in p for p in paths)
    # constants and other framework modules import deal via deal_shim
    assert "plugin/framework/deal_shim.py" in paths
    # Weekly update check (msgbox + HTTP); lazy-imported off the grammar hot path.
    assert "plugin/chatbot/extension_update_check.py" in paths
    assert "plugin/chatbot/dialogs.py" in paths
    assert "plugin/framework/client/requests.py" in paths
    # Dual-install: WriterAgent's chatbot package init imports ModuleBase.
    assert "plugin/framework/module_base.py" in paths
    assert "plugin/chatbot/__init__.py" not in paths


def test_libreharper_config_always_uses_harper_and_ignores_json() -> None:
    from unittest.mock import patch
    from plugin.framework.config import get_grammar_provider, grammar_checker_identity, is_grammar_enabled
    from plugin.framework.constants import EXTENSION_ID_LIBREHARPER
    from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id

    try:
        set_package_extension_id(EXTENSION_ID_LIBREHARPER)
        with patch("plugin.framework.config.get_config", return_value="off"):
            assert is_grammar_enabled() is True
            assert get_grammar_provider() == "harper"
            assert grammar_checker_identity() == "harper"

        with patch("plugin.framework.config.get_config", return_value="llm"):
            assert is_grammar_enabled() is True
            assert get_grammar_provider() == "harper"
            assert grammar_checker_identity() == "harper"
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_identity_and_locale_check() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        # __init__ calls maybe_start_harper_async when user_config_dir is set.
        # The autouse config fixture supplies a real temp dir, so without this
        # patch a unit test downloads/starts harper-ls and Windows xdist hangs.
        with patch("plugin.writer.locale.harper.maybe_start_harper_async"):
            proofreader = HarperProofreader(ctx)
        assert proofreader._checker_identity == "harper"
        assert proofreader._provider == "harper"

        locale_en = SimpleNamespace(Language="en", Country="US", Variant="")
        assert proofreader._check_enabled_and_locale("doc1", "Some text.", locale_en, 0, 10) == "en-US"

        locale_de = SimpleNamespace(Language="de", Country="DE", Variant="")
        assert proofreader._check_enabled_and_locale("doc1", "Some text.", locale_de, 0, 10) is None
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_initialization_starts_harper_warmup() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.config.init_config"),
            patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"),
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
            mock_start.assert_called_once_with(ctx, user_config_dir="/tmp/lo-user")
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_skips_register_side_effects_on_no_vcl() -> None:
    """uno.bin register/enable must not warmup harper-ls or schedule update check (#768)."""
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.uno_context.desktop_create_is_unsafe", return_value=True),
            patch("plugin.chatbot.extension_update_check.schedule_extension_update_check_once") as mock_update,
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
        mock_update.assert_not_called()
        mock_start.assert_not_called()
        ctx.ServiceManager.createInstanceWithContext.assert_not_called()
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_skips_warmup_without_config_dir() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.config.init_config"),
            patch("plugin.framework.config.user_config_dir", return_value=""),
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
            mock_start.assert_not_called()
    finally:
        reset_package_extension_id_for_tests()


def test_libreharper_bundle_covers_chatbot_package_init_for_dual_install() -> None:
    """WriterAgent ``plugin.chatbot`` init imports ModuleBase; slim OXT must ship it.

    ``make deploy-harper`` does not remove WriterAgent. ``plugin/__init__.py``
    uses ``pkgutil.extend_path``, so the full chatbot package init can run
    against LibreHarper's regular ``plugin.framework`` package. The shipped-
    file import graph does not see that parent init because LibreHarper does
    not copy ``plugin/chatbot/__init__.py``.
    """
    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths

    root = _repo_root()
    shipped = set(collect_libreharper_plugin_paths(root))
    init_path = os.path.join(root, "plugin", "chatbot", "__init__.py")
    tree = ast.parse(open(init_path, encoding="utf-8").read(), filename="plugin/chatbot/__init__.py")
    missing: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not (node.module == "plugin" or node.module.startswith("plugin.")):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            rel = node.module.replace(".", "/") + "/" + alias.name + ".py"
            pkg = node.module.replace(".", "/") + "/" + alias.name + "/__init__.py"
            file_mod = node.module.replace(".", "/") + ".py"
            if rel not in shipped and pkg not in shipped and file_mod not in shipped:
                missing.append(f"plugin/chatbot/__init__.py -> from {node.module} import {alias.name}")
    assert missing == []


def test_libreharper_dual_install_can_import_extension_update_check(tmp_path) -> None:
    """Stage slim files + WriterAgent chatbot init; update-check import must work."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths

    root = Path(_repo_root())
    stage = tmp_path / "bundle"
    for rel in collect_libreharper_plugin_paths(str(root)):
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
    shutil.copy2(root / "plugin" / "chatbot" / "__init__.py", stage / "plugin" / "chatbot" / "__init__.py")

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


