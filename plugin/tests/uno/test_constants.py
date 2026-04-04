import sys
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

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
    prompt = get_chat_system_prompt_for_document(model)
    from plugin.framework.constants import DEFAULT_CHAT_SYSTEM_PROMPT
    assert prompt == DEFAULT_CHAT_SYSTEM_PROMPT
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
