from unittest.mock import MagicMock, patch

import pytest

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.prompts import (
    DELEGATION_USER_FILE_DATA_HINT,
    SIDEBAR_VS_DOCUMENT,
    get_greeting_for_document,
    get_chat_system_prompt_for_document,
    get_core_directives,
    get_specialized_delegation_for_model,
    python_specialized_sub_agent_hint,
    WRITER_CORE_DIRECTIVES,
    CALC_CORE_DIRECTIVES,
    DRAW_CORE_DIRECTIVES,
    DEFAULT_WRITER_GREETING,
    DEFAULT_CALC_GREETING,
    DEFAULT_DRAW_GREETING,
)

# NOTE: the EXTERNAL_AGENT_GUIDANCE pin test moved to tests/chatbot/test_agent_manual.py —
# the blob was retired; the single source is the shared prompt pieces in constants.py (the
# sidebar embeds them, get_guidance serves them per topic, full_manual() feeds the agent backend).


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
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT, get_chat_response_format_instructions

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        fmt = get_chat_response_format_instructions(MagicMock())
    assert CHAT_RESPONSE_FORMAT not in fmt
    assert "plain text only" in fmt


def test_get_chat_response_format_instructions_html_when_rich_enabled():
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT, RICH_CHAT_SIDEBAR_INSTRUCTIONS, get_chat_response_format_instructions

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True):
        fmt = get_chat_response_format_instructions(MagicMock())
    assert fmt == RICH_CHAT_SIDEBAR_INSTRUCTIONS
    assert CHAT_RESPONSE_FORMAT in fmt
    assert "&lt;p&gt;Paragraph&lt;/p&gt;" in fmt
    assert "line breaks within an element" in fmt


def test_get_chat_system_prompt_plain_text_when_rich_disabled():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        prompt = get_chat_system_prompt_for_document(model)
    assert CHAT_RESPONSE_FORMAT not in prompt
    assert "plain text only" in prompt
    assert "LibreOffice Writer assistant" in prompt


def test_get_chat_system_prompt_allows_html_when_rich_text_control_sidebar():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT, RICH_CHAT_SIDEBAR_INSTRUCTIONS

    with patch("plugin.framework.config.get_config_bool_safe") as mock_bool:
        mock_bool.side_effect = lambda key: key == "rich_text_control_sidebar"
        prompt = get_chat_system_prompt_for_document(model, ctx=MagicMock())
        assert RICH_CHAT_SIDEBAR_INSTRUCTIONS in prompt
        assert CHAT_RESPONSE_FORMAT in prompt
        assert "plain text only" not in prompt


def test_get_chat_system_prompt_allows_html_by_default_fallback():
    model = MagicMock()
    model.supportsService.return_value = False
    from plugin.framework.config_schema import _get_schema_default, as_bool
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT, RICH_CHAT_SIDEBAR_INSTRUCTIONS

    rich_default = as_bool(_get_schema_default("rich_text_control_sidebar"))

    # When get_config_bool fails, get_config_bool_safe must fall back to the module.yaml default.
    with patch("plugin.framework.config.get_config_bool", side_effect=Exception("Missing key")):
        prompt = get_chat_system_prompt_for_document(model, ctx=MagicMock())

    if rich_default:
        assert RICH_CHAT_SIDEBAR_INSTRUCTIONS in prompt
        assert CHAT_RESPONSE_FORMAT in prompt
        assert "plain text only" not in prompt
    else:
        assert RICH_CHAT_SIDEBAR_INSTRUCTIONS not in prompt
        assert CHAT_RESPONSE_FORMAT not in prompt
        assert "plain text only" in prompt


def test_writer_chat_prompt_opens_with_persona_and_color_guidance():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert "LibreOffice Writer assistant" in prompt
    assert "thoughtful use of color" in prompt


def test_writer_chat_prompt_section_order_matches_assembly():
    """Writer system prompt sections appear in model-facing order (persona → format → tools → HTML last)."""
    from plugin.framework.prompts import (
        CONFIRM_EDITS_FROM_STRUCTURED_FIELDS,
        SIDEBAR_VS_DOCUMENT,
        WRITER_APPLY_DOCUMENT_HTML_RULES,
        WRITER_CHAT_TOOLS_SECTION,
    )

    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    chat_fmt = prompt.index("CHAT RESPONSE FORMAT")
    tools = prompt.index(WRITER_CHAT_TOOLS_SECTION)
    # Use the full apply-HTML block — "APPLY_DOCUMENT_CONTENT AND HTML" also appears
    # as a forward reference inside WRITER_CHAT_TOOLS_SECTION.
    html_rules = prompt.index(WRITER_APPLY_DOCUMENT_HTML_RULES)
    sidebar = prompt.index(SIDEBAR_VS_DOCUMENT)
    confirm = prompt.index(CONFIRM_EDITS_FROM_STRUCTURED_FIELDS)
    assert chat_fmt < tools < sidebar < html_rules < confirm
    assert prompt.index("LibreOffice Writer assistant") < chat_fmt


def test_writer_chat_template_ends_with_apply_html_then_confirm():
    """Recency: apply-HTML then confirm-from-fields are the last two ambient pieces."""
    from plugin.framework.prompts import (
        CONFIRM_EDITS_FROM_STRUCTURED_FIELDS,
        DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE,
        MEMORY_GUIDANCE,
        RESEARCH_COMPLETION_INSTRUCTION_WRITER,
        SIDEBAR_VS_DOCUMENT,
        WRITER_APPLY_DOCUMENT_HTML_RULES,
    )

    template = DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE
    specialized = template.index("{specialized_delegation}")
    memory = template.index(MEMORY_GUIDANCE)
    sidebar = template.index(SIDEBAR_VS_DOCUMENT)
    apply_html = template.index(WRITER_APPLY_DOCUMENT_HTML_RULES)
    confirm = template.index(CONFIRM_EDITS_FROM_STRUCTURED_FIELDS)
    assert specialized < memory < sidebar < apply_html < confirm
    assert template.rstrip().endswith(CONFIRM_EDITS_FROM_STRUCTURED_FIELDS.rstrip())
    after_apply = template[apply_html + len(WRITER_APPLY_DOCUMENT_HTML_RULES):].lstrip()
    assert after_apply.startswith(CONFIRM_EDITS_FROM_STRUCTURED_FIELDS.lstrip())
    # Confirm bullet is not also left inside TOOL_USAGE_PATTERNS (would bury recency).
    assert template.count(CONFIRM_EDITS_FROM_STRUCTURED_FIELDS) == 1
    assert RESEARCH_COMPLETION_INSTRUCTION_WRITER not in template


def test_writer_chat_prompt_includes_sidebar_vs_document_routing():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert SIDEBAR_VS_DOCUMENT in prompt
    assert "apply_document_content" in prompt


def test_writer_chat_prompt_research_delegate_to_document():
    from plugin.framework.prompts import WRITER_SIDEBAR_ONLY_DOMAINS

    model = MagicMock()
    model.supportsService.return_value = False
    block = get_specialized_delegation_for_model(model)
    assert "web_research:" in block
    assert "apply_document_content" in block
    for domain in WRITER_SIDEBAR_ONLY_DOMAINS:
        assert f"{domain}:" not in block


def test_writer_eval_chat_prompt_includes_sidebar_vs_document_routing():
    # Eval prompts live under scripts/, which is not copied into the stripped
    # make release tree (pytest there uses --ignore=tests/scripts).
    pytest.importorskip("scripts.prompt_optimization.eval_prompts")
    from scripts.prompt_optimization.eval_prompts import get_writer_eval_chat_system_prompt

    prompt = get_writer_eval_chat_system_prompt()
    assert SIDEBAR_VS_DOCUMENT in prompt
    assert "apply_document_content" in prompt


def test_writer_apply_document_math_latex_rules_document_only():
    from plugin.framework.prompts import HTML_FRAGMENT_RULES, WRITER_APPLY_DOCUMENT_HTML_RULES

    assert "Math (CRITICAL)" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert r"\(" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert r"\[" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert "Math (display):" not in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert "Math (CRITICAL)" not in HTML_FRAGMENT_RULES
    assert "Local edits use target='search'" in WRITER_APPLY_DOCUMENT_HTML_RULES
    assert "target='full_document' is rewrite/translation only" in WRITER_APPLY_DOCUMENT_HTML_RULES

    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert "Math (CRITICAL)" in prompt


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
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        prompt = get_chat_system_prompt_for_document(model)
    assert CHAT_RESPONSE_FORMAT not in prompt
    assert "plain text only" in prompt
    assert "Calc" in prompt
    assert 'domain="python"' not in prompt
    assert 'domain="analysis"' not in prompt
    assert "=PY" in prompt

def test_get_chat_system_prompt_for_document_draw():
    model = MagicMock()
    def supportsService(service):
        return service in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    model.supportsService.side_effect = supportsService
    from plugin.framework.prompts import CHAT_RESPONSE_FORMAT

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
    assert 'delegate_to_specialized_writer_toolset(domain="document_research")' in directives
    assert 'delegate_to_specialized_writer_toolset(domain="web_research")' in directives
    assert 'delegate_to_specialized_writer_toolset(domain="python")' in directives
    assert DELEGATION_USER_FILE_DATA_HINT in directives


def test_writer_chat_prompt_delegation_routing_local_vs_web():
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model)
    assert DELEGATION_USER_FILE_DATA_HINT in prompt
    assert "to research public topics" in prompt
    assert "OLE in active doc only" in prompt


def test_specialized_delegation_block_is_single_line():
    from plugin.framework.prompts import SPECIALIZED_TASK_RULES, get_specialized_delegation_for_model, get_specialized_delegation_tool_hint
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


def test_calc_core_directives_py_formula_not_domains():
    assert 'domain="python"' not in CALC_CORE_DIRECTIVES
    assert 'domain="analysis"' not in CALC_CORE_DIRECTIVES
    assert "write_formula_range" in CALC_CORE_DIRECTIVES
    assert "write_formula_range of =PY" in CALC_CORE_DIRECTIVES


def test_write_formula_range_description_owns_py_dest_and_spill():
    from plugin.calc.cells import WriteCellRange

    desc = WriteCellRange.description
    assert "J1" in desc
    assert "new sheet" in desc
    assert "circular" in desc
    assert "say where" in desc
    assert "small peek" in desc
    assert "do not dump the input or full spill" in desc
    assert "do not write =PY onto DataRange" in desc
    assert "data.to_pandas().drop_duplicates()" in desc
    assert "mixed cell types" in desc
    assert "multiline CSV from a start cell" in desc


def test_insert_cell_html_description_keeps_border_guidance():
    from plugin.calc.cells import InsertCellHtml

    assert "Use set_style for table-wide borders" in InsertCellHtml.description


def test_calc_formula_syntax_sheet_dot_not_excel_bang():
    from plugin.framework.prompts import _ensure_venv_import_policy_strings

    _ensure_venv_import_policy_strings()
    from plugin.framework.prompts import CALC_FORMULA_SYNTAX, CALC_PYTHON_FORMULA_LLM_HINT

    assert "never Excel bang" in CALC_FORMULA_SYNTAX
    assert "Orders.A1:H500" in CALC_FORMULA_SYNTAX
    assert "#NAME?" in CALC_FORMULA_SYNTAX
    assert "=PY(\"result = …\"; Orders.A1:H500)" in CALC_FORMULA_SYNTAX
    assert "always 2D" in CALC_FORMULA_SYNTAX
    assert "not builtin sum" in CALC_FORMULA_SYNTAX
    assert CALC_PYTHON_FORMULA_LLM_HINT is CALC_FORMULA_SYNTAX


def test_calc_cell_links_use_calc_dot():
    model = MagicMock()

    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"

    model.supportsService.side_effect = supportsService
    prompt = get_chat_system_prompt_for_document(model)
    assert 'href="cell://Orders.A1"' in prompt
    assert "Excel Orders!A1" not in prompt


def test_calc_workflow_warns_large_range_overloads_context():
    from plugin.framework.prompts import CALC_WORKFLOW

    assert "overloads the model context" in CALC_WORKFLOW
    assert "get_sheet_summary" in CALC_WORKFLOW
    assert "pass the A1 address to =PY" in CALC_WORKFLOW


def test_calc_chat_prompt_includes_context_overload_why():
    model = MagicMock()

    def supportsService(service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"

    model.supportsService.side_effect = supportsService
    prompt = get_chat_system_prompt_for_document(model)
    assert "overloads the model context" in prompt
    assert "write_formula_range of =PY" in prompt


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

