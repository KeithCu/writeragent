from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.constants import (
    get_greeting_for_document,
    get_chat_system_prompt_for_document,
    get_core_directives,
    WRITER_CORE_DIRECTIVES,
    CALC_CORE_DIRECTIVES,
    DRAW_CORE_DIRECTIVES,
    DEFAULT_WRITER_GREETING,
    DEFAULT_CALC_GREETING,
    DEFAULT_DRAW_GREETING,
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


def test_writer_chat_prompt_opens_with_persona_and_color_guidance():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert "LibreOffice Writer assistant" in prompt
    assert "thoughtful use of color" in prompt


def test_writer_chat_prompt_fix_this_grammar_defaults():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert '"fix this"' in prompt
    assert "synonym or equivalent" in prompt
    assert "spelling and grammar" in prompt
    assert "current sentence" in prompt
    assert "context" in prompt

def test_get_chat_system_prompt_for_document_calc():
    model = MagicMock()
    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"
    model.supportsService.side_effect = supportsService
    prompt = get_chat_system_prompt_for_document(model)
    from plugin.framework.constants import DEFAULT_CALC_CHAT_SYSTEM_PROMPT
    assert prompt == DEFAULT_CALC_CHAT_SYSTEM_PROMPT
    assert get_chat_system_prompt_for_document(model, "extra") == DEFAULT_CALC_CHAT_SYSTEM_PROMPT + "\n\nextra"

def test_get_chat_system_prompt_for_document_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    prompt = get_chat_system_prompt_for_document(model)
    from plugin.framework.constants import DEFAULT_DRAW_CHAT_SYSTEM_PROMPT
    assert prompt == DEFAULT_DRAW_CHAT_SYSTEM_PROMPT
    assert get_chat_system_prompt_for_document(model, "extra") == DEFAULT_DRAW_CHAT_SYSTEM_PROMPT + "\n\nextra"


def test_get_core_directives_writer():
    model = MagicMock()
    model.supportsService.return_value = False
    directives = get_core_directives(model)
    assert directives == WRITER_CORE_DIRECTIVES
    assert "delegate_to_specialized_writer_toolset" in directives
    assert 'domain="python"' in directives
    assert "apply_document_content" in directives


def test_get_core_directives_calc():
    model = MagicMock()
    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"
    model.supportsService.side_effect = supportsService
    directives = get_core_directives(model)
    assert directives == CALC_CORE_DIRECTIVES
    assert "delegate_to_specialized_calc_toolset" in directives
    assert 'domain="python"' not in directives
    assert "apply_document_content" not in directives


def test_get_core_directives_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    directives = get_core_directives(model)
    assert directives == DRAW_CORE_DIRECTIVES
    assert "delegate_to_specialized_draw_toolset" in directives
    assert 'domain="python"' in directives
    assert "apply_document_content" not in directives
