import pytest
from gettext import NullTranslations
from pathlib import Path
from unittest.mock import MagicMock, patch
import deal
from plugin.framework.deal_shim import DEAL_MAX_MSGID
from plugin.framework.i18n import _, get_lo_locale
import plugin.framework.i18n as i18n_module
import sys


class TestI18n:
    def setup_method(self):
        # Reset i18n initialization state
        i18n_module.reset_i18n()
        self._tmp_dirs = []

    def teardown_method(self):
        for td in getattr(self, "_tmp_dirs", []):
            td.cleanup()

    def test_i18n_fallback(self):
        """With NullTranslations, msgid passes through unchanged."""
        i18n_module._translation = NullTranslations()
        assert (_("ThisIsAnUntranslatedString999")) == ("ThisIsAnUntranslatedString999")

    def test_i18n_msgid_allows_unicode_and_empty(self):
        """gettext msgids are not an ASCII-only domain (deal.pre must not use isascii)."""
        i18n_module._translation = NullTranslations()
        assert (_("✓ Copied!")) == ("✓ Copied!")
        assert (_("Testing…")) == ("Testing…")
        assert (_("")) == ("")

    def test_i18n_msgid_rejects_over_deal_max_msgid(self):
        from tests.harness.strip_bundle import deal_pre_present

        if not deal_pre_present(_):
            pytest.skip("@deal.pre stripped in release bundle")
        i18n_module._translation = NullTranslations()
        with pytest.raises(deal.PreContractError):
            _("x" * (DEAL_MAX_MSGID + 1))

    def test_locale_detection_uno(self):
        """Test locale detection uses LibreOffice ooLocale via UNO."""
        mock_ctx = MagicMock()
        mock_smgr = MagicMock()
        mock_ctx.getServiceManager.return_value = mock_smgr

        mock_config_provider = MagicMock()
        mock_smgr.createInstanceWithContext.return_value = mock_config_provider

        mock_ca = MagicMock()
        mock_ca.getPropertyValue.return_value = "fr-FR"
        mock_config_provider.createInstanceWithArguments.return_value = mock_ca

        mock_uno = MagicMock()
        mock_uno.createUnoStruct.return_value = "mock_struct"

        with patch.dict(sys.modules, {'uno': mock_uno}):
            locale = get_lo_locale(mock_ctx)
            assert (locale) == ("fr_FR")

    def test_background_gettext_does_not_stick_english_catalog(self):
        """A worker _() before init_i18n(ctx) must not freeze the English catalog."""
        import threading

        i18n_module._translation = None
        i18n_module._active_locale = "en_US"
        holder: dict[str, object] = {}

        def run() -> None:
            holder["text"] = _("Hello")
            holder["catalog"] = i18n_module._translation

        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        assert holder["text"] == "Hello"
        assert holder["catalog"] is None
        assert i18n_module._translation is None

        with (
            patch.object(i18n_module, "get_lo_locale", return_value="fr_FR"),
            patch.object(i18n_module, "load_translation", return_value=NullTranslations()) as load,
        ):
            i18n_module.init_i18n(object())
        load.assert_called_once()
        assert i18n_module._active_locale == "fr_FR"
        assert i18n_module._translation is not None
        i18n_module._translation = None

    def test_locale_detection_default_when_uno_fails(self):
        """When UNO/config is unavailable, locale defaults to English (not OS LANG)."""
        mock_ctx = MagicMock()
        mock_ctx.getServiceManager.side_effect = Exception("No UNO")

        locale = get_lo_locale(mock_ctx)
        assert (locale) == ("en_US")

    def test_get_lo_locale_none_ctx_uses_get_ctx_on_main(self):
        from plugin.framework.queue_executor import main

        guarded_ctx = MagicMock(name="guarded_ctx")
        smgr = MagicMock()
        guarded_ctx.getServiceManager.return_value = smgr
        config_provider = MagicMock()
        smgr.createInstanceWithContext.return_value = config_provider
        ca = MagicMock()
        ca.getPropertyValue.return_value = "de-DE"
        config_provider.createInstanceWithArguments.return_value = ca

        mock_uno = MagicMock()
        mock_uno.getComponentContext.side_effect = AssertionError(
            "uno.getComponentContext must not be used; call get_ctx()"
        )
        mock_uno.createUnoStruct.return_value = MagicMock()

        with (
            patch.object(main, "is_current", return_value=True),
            patch("plugin.framework.uno_context.get_ctx", return_value=guarded_ctx) as mock_get_ctx,
            patch.dict(sys.modules, {"uno": mock_uno}),
        ):
            locale = get_lo_locale(None)
        mock_get_ctx.assert_called_once()
        assert locale == "de_DE"

    def test_get_lo_locale_none_ctx_off_main_returns_default(self):
        from plugin.framework.queue_executor import main

        with (
            patch.object(main, "is_current", return_value=False),
            patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx,
        ):
            locale = get_lo_locale(None)
        assert locale == "en_US"
        mock_get_ctx.assert_not_called()

    def test_get_lo_locale_passed_ctx_off_main_returns_default(self):
        from plugin.framework.queue_executor import main

        with patch.object(main, "is_current", return_value=False):
            locale = get_lo_locale(MagicMock())
        assert locale == "en_US"

    def _ensure_mo(self, lang: str, entries: dict[str, str] | None = None) -> str:
        """Build a fixture .mo catalog for *lang* in a temporary directory."""
        import tempfile

        import polib

        catalog = polib.POFile()
        catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8", "Language": lang}
        for msgid, msgstr in (entries or {"Built-in": "Integriert"}).items():
            catalog.append(polib.POEntry(msgid=msgid, msgstr=msgstr))
        tmp_dir = tempfile.TemporaryDirectory()
        self._tmp_dirs.append(tmp_dir)
        target_dir = Path(tmp_dir.name) / lang / "LC_MESSAGES"
        target_dir.mkdir(parents=True, exist_ok=True)
        catalog.save_as_mofile(str(target_dir / "writeragent.mo"))
        return tmp_dir.name

    def test_i18n_translation_loading(self):
        """gettext can load writeragent.mo and translate 'Built-in' using the fixture catalog."""
        localedir = self._ensure_mo("de", {"Built-in": "Integriert"})
        translation = i18n_module.load_translation(["de"], localedir, fallback=False)
        assert (translation.gettext("Built-in")) == ("Integriert")
        assert (translation.gettext("Backend")) == ("Backend")

    def test_i18n_translation_loading_korean(self):
        """gettext can load writeragent.mo and round-trip non-Latin fixture text."""
        localedir = self._ensure_mo("ko", {"Built-in": "내장"})
        translation = i18n_module.load_translation(["ko"], localedir, fallback=False)
        assert (translation.gettext("Built-in")) == ("내장")

    def test_load_translation_opens_explicit_mo_when_find_misses(self):
        """Windows gettext.find can miss locales/<lang>/… even when the .mo exists."""
        localedir = self._ensure_mo("de", {"Built-in": "Integriert"})
        dummy = NullTranslations()
        with patch("plugin.framework.i18n.gettext.translation", return_value=dummy):
            translation = i18n_module.load_translation(["de"], localedir, fallback=False)
        assert (translation.gettext("Built-in")) == ("Integriert")
        assert (type(translation)) is not (NullTranslations)

    def test_mo_candidate_order_exact_stem_prefix(self):
        """Office de-DE looks up de_DE then de (locales/ has de/, not de_DE/)."""
        import os

        cand = i18n_module._mo_candidates

        def mo(tag: str) -> str:
            return os.path.join("/loc", tag, "LC_MESSAGES", "writeragent.mo")

        assert (cand("/loc", "de_DE.UTF-8")) == ([mo("de_DE.UTF-8"), mo("de_DE"), mo("de")])
        assert (cand("/loc", "zh_TW")) == ([mo("zh_TW"), mo("zh")])
        assert (cand("/loc", "ja")) == ([mo("ja")])
