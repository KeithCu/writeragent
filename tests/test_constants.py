import sys
from unittest.mock import MagicMock

# Mock uno and unohelper so document.py can be imported
class MockBase(object):
    pass

class MockXActionListener(object):
    pass

sys.modules['uno'] = MagicMock()
sys.modules['unohelper'] = MagicMock()
sys.modules['unohelper'].Base = MockBase
sys.modules['com'] = MagicMock()
sys.modules['com.sun'] = MagicMock()
sys.modules['com.sun.star'] = MagicMock()
sys.modules['com.sun.star.awt'] = MagicMock()
sys.modules['com.sun.star.awt'].XActionListener = MockXActionListener

from plugin.framework.constants import (
    get_greeting_for_document,
    get_chat_system_prompt_for_document,
    DEFAULT_WRITER_GREETING,
    DEFAULT_CALC_GREETING,
    DEFAULT_DRAW_GREETING,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_CALC_CHAT_SYSTEM_PROMPT,
    DEFAULT_DRAW_CHAT_SYSTEM_PROMPT,
)

def test_get_greeting_for_document_writer():
    model = MagicMock()
    model.supportsService.return_value = False
    assert get_greeting_for_document(model) == DEFAULT_WRITER_GREETING

def test_get_greeting_for_document_calc():
    model = MagicMock()
    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"
    model.supportsService.side_effect = supportsService
    assert get_greeting_for_document(model) == DEFAULT_CALC_GREETING

def test_get_greeting_for_document_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    assert get_greeting_for_document(model) == DEFAULT_DRAW_GREETING

def test_get_chat_system_prompt_for_document_writer():
    model = MagicMock()
    model.supportsService.return_value = False
    assert get_chat_system_prompt_for_document(model) == DEFAULT_CHAT_SYSTEM_PROMPT
    assert get_chat_system_prompt_for_document(model, "extra") == DEFAULT_CHAT_SYSTEM_PROMPT + "\n\nextra"

def test_get_chat_system_prompt_for_document_calc():
    model = MagicMock()
    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"
    model.supportsService.side_effect = supportsService
    assert get_chat_system_prompt_for_document(model) == DEFAULT_CALC_CHAT_SYSTEM_PROMPT
    assert get_chat_system_prompt_for_document(model, "extra") == DEFAULT_CALC_CHAT_SYSTEM_PROMPT + "\n\nextra"

def test_get_chat_system_prompt_for_document_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    assert get_chat_system_prompt_for_document(model) == DEFAULT_DRAW_CHAT_SYSTEM_PROMPT
    assert get_chat_system_prompt_for_document(model, "extra") == DEFAULT_DRAW_CHAT_SYSTEM_PROMPT + "\n\nextra"
