import pytest
import io
import sys

from plugin.framework import uno_bootstrap


class TestEnsureUtf8Stdio:
    def setup_method(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def teardown_method(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def test_reconfigure_ascii_stream_allows_unicode_print(self) -> None:
        buffer = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = ascii_stdout
            with pytest.raises(UnicodeEncodeError):
                print("hello \U0001f44b \u2014")
            buffer.seek(0)
            buffer.truncate(0)

            uno_bootstrap.ensure_utf8_stdio()
            assert (sys.stdout.encoding) == ("utf-8")
            print("hello \U0001f44b \u2014")
            sys.stdout.flush()
            assert (b"hello") in (buffer.getvalue())
        finally:
            sys.stdout = old_stdout

    def test_ensure_utf8_stdio_runs_once(self) -> None:
        buffer = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = ascii_stdout
            uno_bootstrap.ensure_utf8_stdio()
            first_encoding = sys.stdout.encoding
            uno_bootstrap.ensure_utf8_stdio()
            assert (sys.stdout.encoding) == (first_encoding)
        finally:
            sys.stdout = old_stdout


class TestEnsurePluginOnPathDoc:
    def test_ensure_plugin_on_path_has_docstring(self) -> None:
        assert (uno_bootstrap.ensure_plugin_on_path.__doc__)


def test_alias_loader_preserves_real_module_spec_and_path(tmp_path) -> None:
    import importlib.machinery
    import importlib.util

    mod_file = tmp_path / "_test_real_mod.py"
    mod_file.write_text("X = 1\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    real_name = "_test_real_mod"
    try:
        real_mod = importlib.import_module(real_name)
        assert hasattr(real_mod, "__spec__")
        assert real_mod.__spec__.name == real_name
        assert not hasattr(real_mod, "__path__")

        loader = uno_bootstrap.AliasLoader(real_name)
        spec = importlib.machinery.ModuleSpec("writeragent._test_alias", loader)
        spec.submodule_search_locations = []
        alias_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(alias_mod)

        assert real_mod.__spec__.name == real_name
        found = importlib.util.find_spec(real_name)
        assert found is not None
        assert found.name == real_name
        assert not hasattr(real_mod, "__path__")
    finally:
        sys.modules.pop(real_name, None)
        sys.modules.pop("writeragent._test_alias", None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_alias_loader_preserves_package_path(tmp_path) -> None:
    import importlib.machinery
    import importlib.util

    pkg_dir = tmp_path / "_test_real_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("Y = 2\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    real_name = "_test_real_pkg"
    try:
        real_pkg = importlib.import_module(real_name)
        orig_path = list(real_pkg.__path__)
        assert real_pkg.__spec__.name == real_name

        loader = uno_bootstrap.AliasLoader(real_name)
        spec = importlib.machinery.ModuleSpec("writeragent._test_pkg_alias", loader)
        spec.submodule_search_locations = ["/some/fake/location"]
        alias_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(alias_mod)

        assert list(real_pkg.__path__) == orig_path
        assert real_pkg.__spec__.name == real_name
    finally:
        sys.modules.pop(real_name, None)
        sys.modules.pop("writeragent._test_pkg_alias", None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_find_spec_logs_error_and_returns_none(monkeypatch) -> None:
    import importlib.util
    from unittest.mock import patch

    monkeypatch.setattr("plugin.scripting.sandbox.import_authorized", lambda *args, **kwargs: True)

    def fail_find(name: str):
        if name == "plugin.missing":
            raise RuntimeError("simulated find error")
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fail_find)
    with patch("logging.Logger.debug") as mock_debug:
        importer = uno_bootstrap.AliasImporter()
        spec = importer.find_spec("writeragent.missing")
        assert spec is None
        assert mock_debug.called
        assert any("writeragent.missing" in str(call) for call in mock_debug.call_args_list)


def test_writeragent_root_spec_falls_back_to_namespace_loader(monkeypatch) -> None:
    import importlib.util

    def fake_find(name: str):
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find)
    importer = uno_bootstrap.AliasImporter()
    spec = importer.find_spec("writeragent")
    assert spec is not None
    assert isinstance(spec.loader, uno_bootstrap._WriteragentNamespaceLoader)



