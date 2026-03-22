import sys
import os

sys.path.insert(0, os.path.abspath('.'))

def test_backend_translation_normalization():
    from plugin.modules.agent_backend.registry import normalize_backend_id, get_backend
    assert normalize_backend_id('builtin') == 'builtin'
    assert normalize_backend_id('Built-in') == 'builtin'
    assert normalize_backend_id('Eingebaut') == 'builtin'
    assert normalize_backend_id('hermes') == 'hermes'
    assert normalize_backend_id('Hermes') == 'hermes'
    assert normalize_backend_id('claude') == 'claude'
    assert normalize_backend_id('nonexistent') == 'nonexistent'
    assert get_backend('Eingebaut') is not None
