from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.constants import (
    DELEGATION_USER_FILE_DATA_HINT,
    SIDEBAR_VS_DOCUMENT,
    get_greeting_for_document,
    get_chat_system_prompt_for_document,
    get_writer_eval_chat_system_prompt,
    get_core_directives,
    get_specialized_delegation_for_model,
    python_specialized_sub_agent_hint,
    PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE,
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

def test_get_chat_response_format_instructions_plain_when_rich_disabled():
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT, get_chat_response_format_instructions

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        fmt = get_chat_response_format_instructions(MagicMock())
    assert CHAT_RESPONSE_FORMAT not in fmt
    assert "plain text only" in fmt


def test_get_chat_response_format_instructions_html_when_rich_enabled():
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT, RICH_CHAT_SIDEBAR_INSTRUCTIONS, get_chat_response_format_instructions

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True):
        fmt = get_chat_response_format_instructions(MagicMock())
    assert fmt == RICH_CHAT_SIDEBAR_INSTRUCTIONS
    assert CHAT_RESPONSE_FORMAT in fmt
    assert "&lt;p&gt;Paragraph&lt;/p&gt;" in fmt
    assert "line breaks within an element" in fmt


def test_get_chat_system_prompt_plain_text_when_rich_disabled():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        prompt = get_chat_system_prompt_for_document(model)
    assert CHAT_RESPONSE_FORMAT not in prompt
    assert "plain text only" in prompt
    assert "LibreOffice Writer assistant" in prompt


def test_get_chat_system_prompt_allows_html_when_rich_text_control_sidebar():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT, RICH_CHAT_SIDEBAR_INSTRUCTIONS

    with patch("plugin.framework.config.get_config_bool_safe") as mock_bool:
        mock_bool.side_effect = lambda ctx, key: key == "rich_text_control_sidebar"
        prompt = get_chat_system_prompt_for_document(model, ctx=MagicMock())
        assert RICH_CHAT_SIDEBAR_INSTRUCTIONS in prompt
        assert CHAT_RESPONSE_FORMAT in prompt
        assert "plain text only" not in prompt


def test_get_chat_system_prompt_allows_html_by_default_fallback():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.constants import RICH_CHAT_SIDEBAR_INSTRUCTIONS

    # Patch the lower-level get_config_bool to raise exception, testing get_config_bool_safe's fallback to True for rich_text_control_sidebar
    with patch("plugin.framework.config.get_config_bool", side_effect=Exception("Missing key")):
        prompt = get_chat_system_prompt_for_document(model, ctx=MagicMock())
    assert RICH_CHAT_SIDEBAR_INSTRUCTIONS in prompt
    assert "plain text only" not in prompt


def test_writer_chat_prompt_opens_with_persona_and_color_guidance():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert "LibreOffice Writer assistant" in prompt
    assert "thoughtful use of color" in prompt


def test_writer_chat_prompt_includes_sidebar_vs_document_routing():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert SIDEBAR_VS_DOCUMENT in prompt
    assert "apply_document_content" in prompt


def test_writer_eval_chat_prompt_includes_sidebar_vs_document_routing():
    prompt = get_writer_eval_chat_system_prompt()
    assert SIDEBAR_VS_DOCUMENT in prompt
    assert "apply_document_content" in prompt


def test_writer_apply_document_math_latex_rules_document_only():
    from plugin.framework.constants import HTML_FRAGMENT_RULES, WRITER_APPLY_DOCUMENT_HTML_RULES

    assert "Always use inline delimiters" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert r"\(" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert "Math (display):" not in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert "Always use inline delimiters" not in HTML_FRAGMENT_RULES

    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert "Always use inline delimiters" in prompt


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
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        prompt = get_chat_system_prompt_for_document(model)
    assert CHAT_RESPONSE_FORMAT not in prompt
    assert "plain text only" in prompt
    assert "Calc" in prompt

def test_get_chat_system_prompt_for_document_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    from plugin.framework.constants import CHAT_RESPONSE_FORMAT

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        prompt = get_chat_system_prompt_for_document(model)
    assert CHAT_RESPONSE_FORMAT not in prompt
    assert "plain text only" in prompt
    assert "Draw" in prompt


def test_get_core_directives_writer():
    model = MagicMock()
    model.supportsService.return_value = False
    directives = get_core_directives(model)
    assert directives == WRITER_CORE_DIRECTIVES
    assert "delegate_to_specialized_writer_toolset" in directives
    assert 'domain="python"' in directives
    assert "do not answer from memory" in directives
    assert "fast local numeric" in directives
    assert "numpy" not in directives.lower()
    assert 'domain="document_research"' in directives
    assert DELEGATION_USER_FILE_DATA_HINT in directives
    assert "to research public topics" in directives
    assert "returns plain text in `result`" in directives
    assert "format that text as HTML" in directives
    assert "apply_document_content in the same turn" in directives
    assert 'domain="web_research") first to find information' not in directives


def test_writer_chat_prompt_delegation_routing_local_vs_web():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert DELEGATION_USER_FILE_DATA_HINT in prompt
    assert "to research public topics" in prompt
    assert "OLE in active doc only" in prompt


def test_specialized_delegation_block_is_single_line():
    from plugin.framework.constants import SPECIALIZED_TASK_RULES, get_specialized_delegation_for_model, get_specialized_delegation_tool_hint
    from plugin.writer.specialized_base import ToolWriterSpecialBase

    model = MagicMock()
    model.supportsService.return_value = False
    block = get_specialized_delegation_for_model(model)
    assert "SPECIALIZED WRITER" in block
    assert SPECIALIZED_TASK_RULES in block
    assert "Enumerate what must be true" not in block
    assert "\n" not in block
    assert get_specialized_delegation_tool_hint(ToolWriterSpecialBase, "Writer") == block


def test_calc_core_directives_local_before_web():
    assert 'domain="document_research"' in CALC_CORE_DIRECTIVES
    assert DELEGATION_USER_FILE_DATA_HINT in CALC_CORE_DIRECTIVES
    assert 'domain="web_research") first to find information' not in CALC_CORE_DIRECTIVES


def test_draw_core_directives_local_before_web():
    assert 'domain="document_research"' in DRAW_CORE_DIRECTIVES
    assert DELEGATION_USER_FILE_DATA_HINT in DRAW_CORE_DIRECTIVES
    assert 'domain="web_research") first to find information' not in DRAW_CORE_DIRECTIVES


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


# --- Tests for TD1 (uno_bootstrap) ---

def test_ensure_plugin_on_path_is_idempotent():
    """Calling the helper multiple times must not duplicate entries on sys.path."""
    from plugin.framework.uno_bootstrap import ensure_plugin_on_path
    import sys

    before = list(sys.path)
    root1 = ensure_plugin_on_path(__file__, levels_up=3)
    root2 = ensure_plugin_on_path(__file__, levels_up=3)
    after = list(sys.path)

    assert root1 == root2
    # Should not have added duplicate entries
    assert after.count(root1) == before.count(root1) + (1 if root1 not in before else 0)


def test_calc_core_directives_no_math_python_delegation_line():
    assert "do not answer from memory" not in CALC_CORE_DIRECTIVES


def test_calc_core_directives_analysis_delegation():
    assert 'delegate_to_specialized_calc_toolset(domain="analysis")' in CALC_CORE_DIRECTIVES


def test_core_directives_prohibit_asking_user_to_paste():
    # Writer
    assert "MUST NOT ask the user where to find it" in WRITER_CORE_DIRECTIVES
    assert 'delegate_to_specialized_writer_toolset(domain="document_research") once' in WRITER_CORE_DIRECTIVES
    assert "described file(s)" in WRITER_CORE_DIRECTIVES
    # Calc
    assert "MUST NOT ask the user where the file is stored" in CALC_CORE_DIRECTIVES
    assert 'delegate_to_specialized_calc_toolset(domain="document_research") once' in CALC_CORE_DIRECTIVES
    assert "described file(s)" in CALC_CORE_DIRECTIVES
    # Draw
    assert "MUST NOT ask the user where the file is stored" in DRAW_CORE_DIRECTIVES
    assert 'delegate_to_specialized_draw_toolset(domain="document_research") once' in DRAW_CORE_DIRECTIVES
    assert "described file(s)" in DRAW_CORE_DIRECTIVES


def test_python_specialized_sub_agent_hint_writer():
    hint = python_specialized_sub_agent_hint("Writer")
    assert "PYTHON VENV SANDBOX" in hint
    assert "Allowed stdlib in this sandbox" in hint
    assert "sandbox" in hint.lower()
    assert "DO NOT import numpy" in hint
    assert "does not inject spreadsheet" in hint
    assert "data_range or data into run_venv_python_script" not in hint


def test_python_specialized_sub_agent_hint_calc():
    hint = python_specialized_sub_agent_hint("Calc")
    assert "sandbox" in hint.lower()
    assert "DO NOT import numpy" in hint
    assert "data_range" in hint


def test_document_research_multi_file_delegation_in_prompts():
    model = MagicMock()
    model.supportsService.return_value = False
    block = get_specialized_delegation_for_model(model)
    assert "document_research:" in block
    assert "file(s)" in block
    for directives in (WRITER_CORE_DIRECTIVES, CALC_CORE_DIRECTIVES, DRAW_CORE_DIRECTIVES):
        assert "described file(s)" in directives
        assert "once with" in directives or "once with their" in directives

