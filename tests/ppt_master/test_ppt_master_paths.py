# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.contrib.ppt_master.upstream import collect_svg_files_upstream
from plugin.ppt_master.paths import (
    _configured_data_root,
    _dev_clone_data_root,
    data_root_status_for_path,
    find_data_root_in_site_packages,
    resolve_data_root,
)


def _make_outer_clone_layout(tmp_path: Path) -> tuple[Path, Path]:
    outer = tmp_path / "ppt-master"
    inner = outer / "skills" / "ppt-master"
    inner.mkdir(parents=True)
    (inner / "SKILL.md").write_text("# skill", encoding="utf-8")
    (inner / "templates").mkdir()
    (inner / "scripts" / "svg_to_pptx").mkdir(parents=True)
    return outer, inner


def test_dev_clone_data_root_when_repo_present():
    root = _dev_clone_data_root()
    if root is None:
        return  # ppt-master clone not checked out in this environment
    assert (root / "SKILL.md").is_file()
    assert (root / "scripts" / "svg_to_pptx").is_dir()


def test_find_data_root_prefers_dev_clone():
    found = find_data_root_in_site_packages()
    dev = _dev_clone_data_root()
    if dev is None:
        assert found is None or (found / "SKILL.md").is_file()
    else:
        assert found == dev


def test_upstream_collect_svg_files_from_dev_clone(tmp_path: Path):
    dev = _dev_clone_data_root()
    if dev is None:
        return
    proj = tmp_path / "demo"
    (proj / "svg_final").mkdir(parents=True)
    (proj / "svg_final" / "01.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"/>', encoding="utf-8")
    files = collect_svg_files_upstream(proj, dev)
    assert files and files[0].name == "01.svg"


def test_upstream_scripts_path_imports_discovery():
    dev = _dev_clone_data_root()
    if dev is None:
        return
    from plugin.contrib.ppt_master.upstream import _load_pptx_discovery

    mod = _load_pptx_discovery(dev)
    assert mod is not None
    assert callable(getattr(mod, "find_svg_files", None))


def test_resolve_data_root_uses_configured_path(tmp_path: Path, monkeypatch) -> None:
    skill = tmp_path / "skills"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    (skill / "templates").mkdir()
    import plugin.framework.config as config_mod

    monkeypatch.setattr(config_mod, "get_config_str", lambda key: str(skill) if key == "scripting.ppt_master_data_path" else "")
    assert _configured_data_root() == skill
    assert resolve_data_root() == skill


def test_data_root_status_for_path_uses_typed_value(tmp_path: Path) -> None:
    skill = tmp_path / "ppt-master"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    (skill / "templates").mkdir()
    (skill / "scripts").mkdir()
    status = data_root_status_for_path(str(skill))
    assert status["ok"] is True
    assert status["has_skill_md"] is True


def test_configured_data_root_accepts_outer_clone_path(tmp_path: Path, monkeypatch) -> None:
    outer, inner = _make_outer_clone_layout(tmp_path)
    import plugin.framework.config as config_mod

    monkeypatch.setattr(config_mod, "get_config_str", lambda key: str(outer) if key == "scripting.ppt_master_data_path" else "")
    assert _configured_data_root() == inner
    assert resolve_data_root() == inner


def test_data_root_status_for_path_accepts_outer_clone_path(tmp_path: Path) -> None:
    outer, inner = _make_outer_clone_layout(tmp_path)
    status = data_root_status_for_path(str(outer))
    assert status["ok"] is True
    assert status["data_root"] == str(inner)
    assert status["has_svg_to_pptx"] is True


def test_resolve_data_root_env_accepts_outer_clone_path(tmp_path: Path, monkeypatch) -> None:
    outer, inner = _make_outer_clone_layout(tmp_path)
    import plugin.framework.config as config_mod

    monkeypatch.setattr(config_mod, "get_config_str", lambda key: "")
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(outer))
    assert resolve_data_root() == inner

