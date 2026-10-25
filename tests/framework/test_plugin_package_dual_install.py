from __future__ import annotations

from collections.abc import Iterator
import logging
import os
from pathlib import Path

import pytest

import plugin
from plugin import audit_plugin_roots


def _make_plugin_dir(tmp_path: Path, name: str, version: str | None = "0.8.82") -> Path:
    p = tmp_path / name / "plugin"
    p.mkdir(parents=True, exist_ok=True)
    (p / "__init__.py").write_text("", encoding="utf-8")
    if version is not None:
        (p / "version.py").write_text(f'EXTENSION_VERSION = "{version}"\n', encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _reset_audit_state() -> Iterator[None]:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    yield
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False


def test_one_versioned_root_ok(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
    caplog.set_level(logging.WARNING, logger="plugin")

    res = audit_plugin_roots([str(root1)])
    assert res == "ok"
    assert len(caplog.records) == 0


def test_two_roots_same_version_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
    root2 = _make_plugin_dir(tmp_path, "oxt2", "0.8.82")
    caplog.set_level(logging.WARNING, logger="plugin")

    res1 = audit_plugin_roots([str(root1), str(root2)])
    assert res1 == "warn"
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    msg = caplog.records[0].getMessage()
    assert os.path.realpath(str(root1)) in msg
    assert os.path.realpath(str(root2)) in msg
    assert "0.8.82" in msg

    caplog.clear()
    res2 = audit_plugin_roots([str(root1), str(root2)])
    assert res2 == "warn"
    assert len(caplog.records) == 0


def test_two_roots_different_versions_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
    root2 = _make_plugin_dir(tmp_path, "oxt2", "0.8.1")
    caplog.set_level(logging.WARNING, logger="plugin")

    res1 = audit_plugin_roots([str(root1), str(root2)])
    assert res1 == "error"
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    msg = caplog.records[0].getMessage()
    assert os.path.realpath(str(root1)) in msg
    assert os.path.realpath(str(root2)) in msg
    assert "0.8.82" in msg
    assert "0.8.1" in msg
    assert "Run only one OXT version, or update both." in msg

    caplog.clear()
    res2 = audit_plugin_roots([str(root1), str(root2)])
    assert res2 == "error"
    assert len(caplog.records) == 0


def test_symlink_to_directory_ok(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
    sym = tmp_path / "link_plugin"
    sym.symlink_to(root1)
    caplog.set_level(logging.WARNING, logger="plugin")

    res = audit_plugin_roots([str(root1), str(sym)])
    assert res == "ok"
    assert len(caplog.records) == 0


def test_two_dirs_only_one_version_file_ok(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin._logged_audit_key = None
    plugin._logged_audit_to_file = False
    root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
    root2 = _make_plugin_dir(tmp_path, "oxt2", version=None)
    caplog.set_level(logging.WARNING, logger="plugin")

    res = audit_plugin_roots([str(root1), str(root2)])
    assert res == "ok"
    assert len(caplog.records) == 0


def test_re_emit_on_handler_attach(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin_logger = logging.getLogger("plugin")
    saved_handlers = list(plugin_logger.handlers)
    plugin_logger.handlers.clear()
    try:
        plugin._logged_audit_key = None
        plugin._logged_audit_to_file = False
        root1 = _make_plugin_dir(tmp_path, "oxt1", "0.8.82")
        root2 = _make_plugin_dir(tmp_path, "oxt2", "0.8.82")
        caplog.set_level(logging.WARNING, logger="plugin")

        res1 = audit_plugin_roots([str(root1), str(root2)])
        assert res1 == "warn"
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING

        null_handler = logging.NullHandler()
        plugin_logger.addHandler(null_handler)
        try:
            caplog.clear()
            res2 = audit_plugin_roots([str(root1), str(root2)])
            assert res2 == "warn"
            assert len(caplog.records) == 1
            assert caplog.records[0].levelno == logging.WARNING

            caplog.clear()
            res3 = audit_plugin_roots([str(root1), str(root2)])
            assert res3 == "warn"
            assert len(caplog.records) == 0
        finally:
            plugin_logger.removeHandler(null_handler)
    finally:
        plugin_logger.handlers = saved_handlers
        plugin._logged_audit_key = None
        plugin._logged_audit_to_file = False
