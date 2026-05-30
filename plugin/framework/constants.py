# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

from enum import IntFlag

import os

APP_REFERER = "https://github.com/KeithCu/writeragent"
APP_TITLE = "WriterAgent"
USER_AGENT = f"{APP_TITLE} ({APP_REFERER})"


def get_plugin_dir():
    """Returns the absolute path to the plugin/ directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_locales_dir():
    """Absolute path to gettext ``locales/`` (sibling of ``plugin/`` in repo and in the .oxt bundle)."""
    return os.path.join(os.path.dirname(get_plugin_dir()), "locales")


PLUGIN_DIR = get_plugin_dir()


# Model capabilities bitmasks (compatible with OnlyOfficeAI values)
class ModelCapability(IntFlag):
    NONE = 0
    CHAT = 1
    IMAGE = 2
    EMBEDDINGS = 4
    AUDIO = 8
    MODERATIONS = 16
    REALTIME = 32
    CODE = 64
    VISION = 128
    TOOLS = 256

# Toggle for specialized delegation approach.
# Approach A: The Sub-Agent Model (True) - Spins up a separate agent.
# Approach B: In-Place Tool Switching (False) - Switches the main model's tools.
USE_SUB_AGENT = True

# Browser-style user agent for a small, whitelisted set of sites
# (e.g. DuckDuckGo and Wikipedia) that expect a real browser UA.
BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0"

# Single Writer-chat source for apply_document_content parameters + HTML shape (TOOLS points here).
WRITER_APPLY_DOCUMENT_HTML_RULES = """
APPLY_DOCUMENT_CONTENT AND HTML (CRITICAL):
- Parameters: `content` and `target` (required). If target='search', also `old_content` (find/replace; HTML in old_content is matched as plain text).
- Targets: 'beginning', 'end', 'selection', 'full_document' (replaces all), or 'search'.
- `content` must be a JSON array of HTML strings (one fragment per heading/paragraph). We wrap in <html>/<body>.
- Use <br> for line breaks within an element; <p> for paragraphs. Raw Unicode (é, ü, ©); straight double quotes ("), not curly/smart quotes or HTML entities. Send <h1> not &lt;h1&gt;. Preserve intentional spacing.
- Math: Always structured math for equations (native LibreOffice objects). Inline: `\\(`…`\\)` or `$`…`$`; display: `$$`…`$$` or `\\[`…`\\]`. Prefer `\\(` over `$` (currency). TeX preferred; MathML in `<math …>` if you already have it. Avoid `$`+digit. No images or informal plain-text formulas.

EXAMPLES:
- Good: ["<h1>Title</h1>", "<p>Paragraph with <strong>bold</strong> text and \\"quotes\\".</p>"]
- Good math: ["<p>Inline \\(x^2\\) and display $$\\frac{1}{2}$$.</p>"]
- Bad: <h1>Title</h1><p>Paragraph</p> (must be a list of strings)
- Bad: ["&lt;h1&gt;Title&lt;/h1&gt;"] (escaped entities)
- Bad: ["# Title", "Paragraph"] (No Markdown)
- Bad: ["&ldquo;Smart quotes&rdquo;"] (use straight quotes ")"""

FORMATTING_RULES = WRITER_APPLY_DOCUMENT_HTML_RULES

# Prepended to the first string `system` message in LlmClient for non-release bundles only
# (``make build`` includes ``plugin/tests``; ``make release`` / ``--no-tests`` does not).
# See `should_prepend_dev_llm_system_prefix()`.
LLM_DEV_BUILD_SYSTEM_PREFIX = (
    "[WriterAgent development build]\n"
    "You are running a development version of the WriterAgent extension. The user is a plugin developer. "
    "If you run into a problem, explain in detail what failed and why so they can improve the extension. "
    "If they ask detailed questions about tool-calling APIs, prompts, or how the software works, answer helpfully so developers can improve the plugin."
)


def should_prepend_dev_llm_system_prefix() -> bool:
    """True when this bundle includes test modules (same signal as the optional Debug / in-OXT tests)."""
    try:
        import importlib.util

        return importlib.util.find_spec("plugin.tests.uno") is not None
    except Exception:
        return False


# Research routing (short); domain bullets use these strings as-is.
DELEGATION_USER_FILE_DATA_HINT = "to use information that is not in the current document, and may be in (my / our) personal or business documents"
DELEGATION_PUBLIC_WEB_HINT = "to research public topics"


def delegation_math_to_python_hint(*, delegate_toolset: str) -> str:
    """Writer/Draw: route computational math to the python specialized sub-agent (fast local venv)."""
    return (
        "For computational or numeric math (exact values, primes, statistics, symbolic algebra, or non-trivial calculation), "
        f'do not answer from memory—use {delegate_toolset}(domain="python") for fast local numeric computation.'
    )


# General directives shared across all AI interfaces
WRITER_CORE_DIRECTIVES = f"""When asked to answer a question or create or explain something, assume the user wants the
information to be inserted into the document. Use the apply_document_content tool to insert content
into LibreOffice so the user can edit it further.
When the user wants {DELEGATION_USER_FILE_DATA_HINT}:
- You MUST NOT ask the user where to find it, or to upload, paste, its contents.
- You MUST call delegate_to_specialized_writer_toolset(domain="document_research") once with their described file(s) and task in task; the sub-agent lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_writer_toolset(domain="web_research").

{delegation_math_to_python_hint(delegate_toolset="delegate_to_specialized_writer_toolset")}
When asked to make a script or run Python, use delegate_to_specialized_writer_toolset(domain="python")."""

CALC_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (including when the user refers to any other file, document, spreadsheet, or sheet by name or path, e.g. "my spreadsheet", "read cell a9 from PythonInCalc", "summary.odt", etc., or asks to pull, read, search, or reference data from them):
- You MUST NOT ask the user where the file is stored, how to find it, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_calc_toolset(domain="document_research") once with their described file(s) and task in task; the sub-agent lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_calc_toolset(domain="web_research")."""

DRAW_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (including when the user refers to any other file, document, spreadsheet, or sheet by name or path, e.g. "my spreadsheet", "read cell a9 from PythonInCalc", "summary.odt", etc., or asks to pull, read, search, or reference data from them):
- You MUST NOT ask the user where the file is stored, how to find it, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_draw_toolset(domain="document_research") once with their described file(s) and task in task; the sub-agent lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_draw_toolset(domain="web_research").

{delegation_math_to_python_hint(delegate_toolset="delegate_to_specialized_draw_toolset")}
When asked to make a script or run Python, use delegate_to_specialized_draw_toolset(domain="python")."""

CORE_DIRECTIVES = WRITER_CORE_DIRECTIVES


def get_core_directives(model) -> str:
    """Return the application-specific core directives dynamically based on document type."""
    from plugin.doc.document_helpers import is_calc, is_draw
    if is_calc(model):
        return CALC_CORE_DIRECTIVES
    elif is_draw(model):
        return DRAW_CORE_DIRECTIVES
    else:
        return WRITER_CORE_DIRECTIVES


TRANSLATION_RULES = "TRANSLATION: get_document_content(scope=full) -> translate -> apply_document_content(target='search', old_content=original, content=translated). Never refuse."

# Tool-usage workflow patterns (no repeat of apply_document_content targets; see WRITER_APPLY_DOCUMENT_HTML_RULES).
TOOL_USAGE_PATTERNS = """TOOL USAGE PATTERNS:
- search_in_document (with return_offsets if needed) is for inspection/navigation; use apply_document_content with old_content for replacements.
- If a tool call fails, verify content and target are provided (use target='beginning' / 'end' / 'selection' for insert-only).
- When asked to review or give feedback or suggestions on a document, use the add_comment method to add your input to specific places in the document. Use for both positive and negative feedback.
- If the user says "fix this" (or a synonym or equivalent in another language with the same intent), assume they want you to correct spelling and grammar errors in the current sentence only, unless the context makes it clear there is another specific error they want you to fix.
"""
# Shared Calc instruction blocks
CALC_WORKFLOW = """WORKFLOW:
1. Understand what the user wants.
2. If needed, use get_sheet_summary or read_cell_range to see the current state.
3. Use the tools to perform the operation. Always use ranges for multiple cells to reduce calls and improve efficiency.
4. Give a short confirmation; when you changed cells, mention the range or addresses (e.g. "Wrote totals in B5:B8")."""

# Shared venv Python prompt text (run_venv_python_script, =PYTHON(), delegate domain=python).
PYTHON_VENV_AUTO_IMPORTS_ALIASES = "`numpy` (as `np`), `sympy` (as `sp`), `pandas` (as `pd`), and standard library `math`"

PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE = f"Note: {PYTHON_VENV_AUTO_IMPORTS_ALIASES} are automatically imported. "

PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE = (
    f"Note: {PYTHON_VENV_AUTO_IMPORTS_ALIASES} are automatically imported. "
    "DO NOT IMPORT numpy, pandas, sympy, or math. "
    "Prefer np/sp/pd (and scipy when appropriate) over hand-rolled Python; you have access to a complete high-performance SciPy install."
)


def python_specialized_sub_agent_hint(agent_label: str) -> str:
    """Smol sub-agent instructions suffix for delegate_to_specialized_* (domain=\"python\")."""
    if agent_label == "Calc":
        data_hint = " You may pass data_range or data into run_venv_python_script so the script receives variable `data`."
    else:
        data_hint = " run_venv_python_script does not inject spreadsheet `data`—use document tools for content."
    return f" PYTHON (venv): {PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE}{data_hint}"


CALC_FORMULA_SYNTAX = f"""FORMULA SYNTAX: LibreOffice uses semicolon (;) as the formula argument separator in formulas.
- Correct: =SUM(A1:A10), =IF(A1>0;B1;C1)
- Wrong: =SUM(A1,A10), =IF(A1>0,"Yes","No") (no commas in formulas)
- Write `=PYTHON("result = ..."; A1:A10)` in cells to calculate/run Python (omit the second argument if no data is needed, e.g. `=PYTHON("result = 2**10")`).
Note: this code executes in an isolated sandbox with no direct access to LibreOffice data, so it must be passed in.
{PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE}
- Example: `=PYTHON("result = np.sum(data)"; A1:A10)`.

"""

MEMORY_GUIDANCE = """MEMORY:
You have a persistent file-backed memory tool.
WHEN TO SAVE (do this proactively, don't wait to be asked):
- User corrects you.
- You discover something about the environment.
Prioritize what reduces future user steering."""

# Brief hint for gateway tool JSON schemas (full rules: SPECIALIZED_TASK_RULES).
DELEGATE_SPECIALIZED_TASK_PARAM_HINT = "Instructions for the sub-agent: it has the full tool/API surface for this domain (all parameters). Be specific enough to use that power—vague tasks leave choices underspecified."

# Shared guidance for writing good `task` strings when delegating to specialized sub-agents.
# This is the main source of duplication we are trying to reduce in the system prompt.
SPECIALIZED_TASK_RULES = (
    "Rules for `task`: Treat it as a complete natural-language specification, not a summary. "
    "Enumerate what must be true (types, layout, numbers, colors, style names, anchors, text). "
    "If the user was vague, state explicit defaults in the task rather than leaving them undefined. "
    "Prefer **concrete, capability-rich** instructions over \"minimal\" or \"basic\" when the user is open to it: "
    "name specific variants (e.g. exact shape presets, styles, or operations) so the sub-agent can use the full API instead of picking a boring default. "
    "Example (domain=shapes): `upsert_shape` can use on the order of **400+** distinct preset `shape_type` strings. "
    "Example (domain=footnotes): Quote the **exact** document sentence or unique substring where the note must attach so the sub-agent can know where to put the footnote anchor."
)

# Shape catalog size: LibreOffice core maps ~400+ preset names (e.g. svx EnhancedCustomShapeTypeNames.cxx).
# Single-line blocks: MCP tool descriptions and many clients do not render newlines inside JSON strings.
WRITER_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED WRITER (nested tools): The default tool list hides deep Writer features. "
    "When the user needs those, call delegate_to_specialized_writer_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the sub-agent must do. The sub-agent only sees tools for that domain, "
    "but they are the real tools: **full parameter lists and full LibreOffice/UNO access** for that area (nothing is dumbed down for the sub-agent). "
    "document_research: use for information in other personal/business documents in the same folder (one delegation per file set). "
    "web_research: use for public web topics. "
    f"{SPECIALIZED_TASK_RULES}"
)

CALC_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED CALC (nested tools): The default tool list hides advanced Calc features. "
    "When the user needs those, call delegate_to_specialized_calc_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the sub-agent must do. The sub-agent has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)

DRAW_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED DRAW (nested tools): The default tool list hides advanced Draw/Impress features. "
    "When the user needs those, call delegate_to_specialized_draw_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the sub-agent must do. The sub-agent has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)

CHAT_RESPONSE_FORMAT = """CHAT RESPONSE FORMAT: Format your conversational responses as HTML (use <p>, <strong>, <em>, <code>, <ul>, <ol>, <h2>, <pre>, <br>). Do NOT use Markdown formatting (no #, **, ```, etc.) in chat responses. The sidebar renders HTML natively."""

DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE = f"""You are a LibreOffice Writer assistant who produces polished, professional documents with thoughtful use of color and formatting.
Honor any stated memory preferences for color, etc.

{CHAT_RESPONSE_FORMAT}

{{core_directives}}

TOOLS:
- apply_document_content: Insert or replace HTML in the document (parameters and format — see APPLY_DOCUMENT_CONTENT AND HTML below).
- get_document_content: Read document (full/selection/range) as HTML.
- search_in_document: Find text (use return_offsets for character positions if needed for inspection).
- apply_style: Apply a paragraph or character style (family='ParagraphStyles' or 'CharacterStyles').

{TRANSLATION_RULES}

{TOOL_USAGE_PATTERNS}

{FORMATTING_RULES}

{{specialized_delegation}}

# {MEMORY_GUIDANCE}
"""

# We dynamically set this later when calling get_chat_system_prompt_for_document
DEFAULT_CHAT_SYSTEM_PROMPT = ""
DEFAULT_CALC_CHAT_SYSTEM_PROMPT = ""


def get_writer_eval_chat_system_prompt() -> str:
    """Writer chat-style system prompt for offline DSPy eval (`scripts/prompt_optimization`).

    Reuses the same HTML / apply_document_content rules as production chat
    (`FORMATTING_RULES`, `TRANSLATION_RULES`) but describes only tools implemented in the
    eval harness: ``get_document_content``, ``apply_document_content``, ``find_text``.
    Omits web research, specialized delegation, memory, and tools not wired in ``tools_lo``.
    """
    eval_scope = "[Eval harness] Only get_document_content, apply_document_content, and find_text are registered. Do not use web research, delegate_to_specialized_writer_toolset, search_in_document, apply_style, or add_comment."
    eval_tool_patterns = """TOOL USAGE PATTERNS (eval harness):
- Use find_text to locate passages; use apply_document_content (often with old_content) to replace HTML.
- Re-read with get_document_content after substantive edits if needed."""
    core_eval = """When asked to answer a question or create or explain something, assume the user wants the
information to be inserted into the document. Use the apply_document_content tool to insert content
into LibreOffice so the user can edit it further."""
    return f"""{core_eval}

{eval_scope}

TOOLS (eval harness):
- apply_document_content: Insert or replace HTML in the document (parameters and format — see APPLY_DOCUMENT_CONTENT AND HTML below).
- get_document_content: Read document (full/selection/range) as HTML.
- find_text: Find text in the document (JSON ranges).

{TRANSLATION_RULES}

{eval_tool_patterns}

{FORMATTING_RULES}
"""


# NOTE: Experimental planning/todo guidance (commented out).
# When the hermes-style `todo` tool is enabled, you can append guidance like:
#
# TASK PLANNING:
# - For complex requests (3+ steps or multiple tasks), call the `todo` tool
#   to create a task list before editing the document.
# - Each item: {id: string, content: string, status: pending|in_progress|completed|cancelled}.
# - Only ONE item should be in_progress at a time.
# - Mark items completed immediately when done; cancel tasks that are no longer needed.
# - For simple, one-off edits, you may skip the todo tool and act directly.


# Calc spreadsheet prompt (structure inspired by libre_calc_ai prompt_templates.py:
# workflow, grouped tools, "do not explain—do the operation", specify addresses).
DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE = f"""You are a LibreOffice Calc spreadsheet assistant who creates polished, professional, and colorful spreadsheets.
Do not explain, do the operation directly using tools. Perform as many steps as needed in one turn when possible.

{CHAT_RESPONSE_FORMAT}

{CALC_WORKFLOW}

{CALC_FORMULA_SYNTAX}

CSV DATA: Use comma (,) for write_formula_range.

TOOLS (grouped by use):

READ:
- read_cell_range: Read values from a cell or range (e.g. A1:D10).
- get_sheet_summary: Summary of the active sheet (size, headers, used range, charts, annotations, merges).

WRITE & FORMAT:
- write_formula_range: Single string fills entire range; JSON array must match range size exactly (one value per cell). Alternatively, provide multiline CSV data to bulk insert starting at a cell. Use empty string/array to clear contents. Use ranges for efficiency; avoid single-cell operations.
- set_style: Use for one or more cells/ranges at once (same formatting applied per range). Good after bulk writes for uniform look. It only exposes a small fixed set of properties (see list below)—not mixed rich text inside a cell. For per-character formatting, links, or HTML structure in a single cell, use insert_cell_html instead.
- set_style properties (each optional except range_name): range_name (array of addresses/ranges); bold; italic; font_size (points); bg_color; font_color (hex #RRGGBB or names: red, yellow, …); h_align (left|center|right|justify); v_align (top|center|bottom); wrap_text; border_color (outline around the range); number_format (e.g. #,##0.00, 0%, dates).
- insert_cell_html: Paste HTML into one cell on the active sheet as rich text (bold, italic, links, line breaks—same import as Writer). Plain write_formula_range cannot do this. One cell only; no images. Does not replace set_style for whole-table borders/number formats—combine as needed.

- merge_cells: Merge a range (e.g. headers); then write and style with write_formula_range/set_style.
- delete_structure: Remove rows or columns at specific positions.

{{specialized_delegation}}

{{core_directives}}"""

DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE = """You are a LibreOffice Draw/Impress assistant who creates polished, professional, and colorful visual content.
Do not explain - do the operation directly using tools. Perform as many steps as needed in one turn when possible.

""" + CHAT_RESPONSE_FORMAT + """

WORKFLOW:
1. Understand the user's request.
2. If needed, use list_pages to understand the current layout.
3. Use the specialized delegation tool to perform shape operations (create, edit, group, etc.).
4. Give a short confirmation (e.g. "Changed rectangle color to red").

COORDINATES:
All coordinates (x, y, width, height) are in 100ths of a millimeter.
A typical page is roughly 21000 x 29700 (A4).

TOOLS:
- list_pages: List all pages/slides in the document.

{specialized_delegation}

{core_directives}"""


# We dynamically set these later when calling get_chat_system_prompt_for_document
DEFAULT_CHAT_SYSTEM_PROMPT = ""
DEFAULT_CALC_CHAT_SYSTEM_PROMPT = ""
DEFAULT_DRAW_CHAT_SYSTEM_PROMPT = ""


def _get_specialized_domains_str(base_cls, *, agent_label: str | None = None) -> str:
    """Build a compact domain list for delegation hints and MCP schemas."""
    parts = []
    for cls in base_cls.__subclasses__():
        domain = getattr(cls, "specialized_domain", None)
        desc = getattr(cls, "specialized_domain_description", None)
        if domain:
            if agent_label == "Calc" and domain == "python":
                continue
            if desc:
                parts.append(f"{domain}: {desc}")
            else:
                parts.append(domain)
    return "; ".join(sorted(parts))


def _specialized_delegation_template_for_label(agent_label: str) -> str:
    if agent_label == "Calc":
        return CALC_SPECIALIZED_DELEGATION_TEMPLATE
    if agent_label == "Draw":
        return DRAW_SPECIALIZED_DELEGATION_TEMPLATE
    return WRITER_SPECIALIZED_DELEGATION_TEMPLATE


def get_specialized_delegation_for_model(model) -> str:
    """Specialized-delegation block for chat system prompt (same text as MCP delegate tool hint)."""
    from plugin.doc.document_helpers import is_calc, is_draw

    if is_calc(model):
        from plugin.calc.base import ToolCalcSpecialBase

        return get_specialized_delegation_tool_hint(ToolCalcSpecialBase, "Calc")
    if is_draw(model):
        from plugin.draw.base import ToolDrawSpecialBase

        return get_specialized_delegation_tool_hint(ToolDrawSpecialBase, "Draw")
    from plugin.writer.specialized_base import ToolWriterSpecialBase

    return get_specialized_delegation_tool_hint(ToolWriterSpecialBase, "Writer")


def format_specialized_domains_description(special_base_class, *, agent_label: str | None = None) -> str:
    """Domain enum help for MCP/OpenAPI (more compact than the full delegation hint)."""
    domains = _get_specialized_domains_str(special_base_class, agent_label=agent_label)
    if not domains:
        return "The specialized domain to activate."
    # Compact form for the enum property description to reduce bloat in MCP schema
    compact = domains.replace("; ", ", ")
    return f"domain one of: {compact}"


def get_specialized_delegation_tool_hint(special_base_class, agent_label: str) -> str:
    """Full specialized-delegation guidance (sidebar system prompt and MCP ``tools/list``)."""
    domains_str = _get_specialized_domains_str(special_base_class, agent_label=agent_label)
    template = _specialized_delegation_template_for_label(agent_label)
    return template.format(domains=domains_str)


# Dummy gettext function for string extraction tools (xgettext)
# We don't evaluate them here to avoid early localization issues.
def _(x):
    return x


DEFAULT_WRITER_GREETING = _("AI: I can edit or translate your document instantly with professional formatting and color. Try me!")
DEFAULT_CALC_GREETING = _("AI: I can help you with formulas, data analysis, and colorful charts. Try me!")
DEFAULT_DRAW_GREETING = _("AI: I can help you create and edit polished, colorful shapes in Draw and Impress. Try me!")
DEFAULT_RESEARCH_GREETING = _("AI: I can do web research to answer any question, or summarize a web page, without seeing or changing your document. Let's chat.")

# Remove dummy _ so it doesn't leak
del _


def get_greeting_for_document(model):
    """Return a greeting relevant to the document type."""
    from plugin.framework.i18n import _
    from plugin.doc.document_helpers import is_calc, is_draw

    if is_calc(model):
        return _(DEFAULT_CALC_GREETING)
    elif is_draw(model):
        return _(DEFAULT_DRAW_GREETING)
    else:
        return _(DEFAULT_WRITER_GREETING)


def get_chat_system_prompt_for_document(model, additional_instructions="", ctx=None):
    """Single source of truth for chat system prompt. Use this so Writer vs Calc prompt cannot be mixed.
    model: document model (Writer, Calc, or Draw). additional_instructions: optional extra text appended.
    Callers must pass the document that is being chatted about."""
    from plugin.doc.document_helpers import is_calc, is_draw

    delegation = get_specialized_delegation_for_model(model)

    if is_calc(model):
        base = DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE.replace("{specialized_delegation}", delegation)
        base = base.replace("{core_directives}", CALC_CORE_DIRECTIVES)

        global DEFAULT_CALC_CHAT_SYSTEM_PROMPT
        if not DEFAULT_CALC_CHAT_SYSTEM_PROMPT:
            DEFAULT_CALC_CHAT_SYSTEM_PROMPT = base
    elif is_draw(model):
        base = DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE.replace("{specialized_delegation}", delegation)
        base = base.replace("{core_directives}", DRAW_CORE_DIRECTIVES)

        global DEFAULT_DRAW_CHAT_SYSTEM_PROMPT
        if not DEFAULT_DRAW_CHAT_SYSTEM_PROMPT:
            DEFAULT_DRAW_CHAT_SYSTEM_PROMPT = base
    else:
        base = DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE.replace("{specialized_delegation}", delegation)
        base = base.replace("{core_directives}", WRITER_CORE_DIRECTIVES)

        # update the static variable once it's lazily generated so tests and imports works
        global DEFAULT_CHAT_SYSTEM_PROMPT
        if not DEFAULT_CHAT_SYSTEM_PROMPT:
            DEFAULT_CHAT_SYSTEM_PROMPT = base

    from plugin.chatbot.rich_text import is_embedded_rich_text_sidebar_enabled
    from plugin.framework.config import get_config_bool_safe

    if not (
        is_embedded_rich_text_sidebar_enabled(ctx)
        or get_config_bool_safe(ctx, "rich_text_control_sidebar", default=True)
    ):
        base = base.replace(CHAT_RESPONSE_FORMAT, "CHAT RESPONSE FORMAT: Respond in plain text only. Do NOT use HTML tags or Markdown formatting (no #, **, ```, etc.).")

    if ctx:
        try:
            from plugin.chatbot.memory import MemoryStore

            store = MemoryStore(ctx)
            user_mem = store.read("user")
            if user_mem:
                base += "\n\n[USER PROFILE / MEMORY]\n" + user_mem.strip() + "\n"
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug(f"Failed to read user memory for prompt: {e}")

    if additional_instructions and str(additional_instructions).strip():
        return base + "\n\n" + str(additional_instructions).strip()
    return base


# Prepend these in venv_sandbox when the module is available and not already imported.
AUTO_IMPORTS: dict[str, str] = {
    "numpy": "import numpy as np",
    "pandas": "import pandas as pd",
    "sympy": "import sympy as sp",
    "math": "import math",
}
