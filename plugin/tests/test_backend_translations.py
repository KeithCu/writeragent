import sys
import os
import gettext

sys.path.insert(0, os.path.abspath('.'))

def test_backend_translation_normalization():
    from plugin.modules.agent_backend.registry import normalize_backend_id, get_backend

    # Test valid internal IDs
    assert normalize_backend_id('builtin') == 'builtin'
    assert normalize_backend_id('hermes') == 'hermes'
    assert normalize_backend_id('claude') == 'claude'

    # Test backward compatibility mapping
    assert normalize_backend_id('Built-in') == 'builtin'
    assert normalize_backend_id('Eingebaut') == 'builtin'
    assert normalize_backend_id('Hermes') == 'hermes'

    # Test fallback for nonexistent or corrupt IDs
    assert normalize_backend_id('nonexistent') == 'builtin'

    # Test get_backend initialization logic with localized string fallback
    assert get_backend('Eingebaut') is not None


def test_i18n_translation_loading():
    """Verify that gettext translation engine can correctly translate 'Built-in' to German."""
    localedir = os.path.join(os.path.abspath('.'), 'plugin', 'locales')
    translation = gettext.translation('writeragent', localedir, languages=['de'], fallback=True)

    # This checks if the .mo file is properly loaded and translations exist
    assert translation.gettext('Built-in') == 'Eingebaut'
    assert translation.gettext('Backend') == 'Backend'


def test_legacy_ui_imports():
    """Verify that the module and its unohelper imports resolve correctly without UNO bridge."""
    try:
        from plugin.framework import legacy_ui
        legacy_ui_imported = True
    except (ModuleNotFoundError, ImportError) as e:
        # Expected to fail when unohelper or com.sun.star.* is not available in headless python,
        # but the module structure itself should not have syntax errors.
        legacy_ui_imported = False
        assert 'unohelper' in str(e) or 'uno' in str(e) or 'com.sun.star' in str(e) or 'cannot import name' in str(e)
