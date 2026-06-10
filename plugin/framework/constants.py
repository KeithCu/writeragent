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

# Max characters of Writer document text embedded in chat system context (excerpt, not model window).
CHAT_DOCUMENT_CONTEXT_MAX_CHARS = 8000

# Local sentence-transformers default until multi-model bench picks a winner (docs/embeddings.md).
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDINGS_WORKER_SESSION_PREFIX = "embeddings"
# Background folder index tick when DOCUMENT_RESEARCH_SEARCH_MODE=embeddings (docs/embeddings.md).
EMBEDDINGS_INDEX_INTERVAL_S = 300
# Warm venv worker pools (docs/embeddings.md — dedicated embeddings subprocess).
WORKER_POOL_DEFAULT = "default"
WORKER_POOL_EMBEDDINGS = "embeddings"
# In-worker read-through corpus matrix cache TTL (seconds since last access).
EMBEDDINGS_CORPUS_CACHE_TTL_S = 60

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

# document_research cross-file discovery: "grep" (default) or "embeddings" (search_embeddings only).
# Edit before make release; no Settings UI in Phase B. See docs/embeddings.md.
DOCUMENT_RESEARCH_SEARCH_MODE = "grep"


def document_research_uses_embeddings() -> bool:
    """True when outer document_research exposes search_embeddings instead of grep_nearby_files."""
    return DOCUMENT_RESEARCH_SEARCH_MODE.strip().lower() == "embeddings"


# Browser-style user agent for a small, whitelisted set of sites
# (e.g. DuckDuckGo and Wikipedia) that expect a real browser UA.
BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0"

# Shared HTML fragment hygiene (sidebar chat + document apply_document_content fragments).
# Tag-level rules are the same; the *container* differs — see comments on
# WRITER_APPLY_DOCUMENT_HTML_RULES vs CHAT_SIDEBAR_HTML_EXAMPLES below.
HTML_FRAGMENT_RULES = """
- Use <br> for line breaks within an element; <p> for paragraphs.
- Raw Unicode (é, ü, ©); straight double quotes ("), not curly/smart quotes or HTML entities. Send <h1> not &lt;h1&gt;. Preserve intentional spacing.
- Do NOT use Markdown (#, **, ```, etc.)."""

# Document tool only (apply_document_content). Not used for sidebar chat or web-research final_answer.
#
# Why a JSON array of strings (not one HTML blob)?
# - OpenAI tool schema declares content as type "array" (content.py). Each element is one
#   heading/paragraph fragment; execute() joins with "\\n" before Writer HTML import.
# - Long answers: block-sized strings are easier for models to emit than one giant tool
#   argument, and JSON parsers handle quotes per element instead of one nested escape soup.
# - That was the original intent; whether it still wins vs a single string is unsettled.
#
# Two different "escaping" layers (models often mix these up):
# - JSON/tool-call escaping: inside a tool argument, literal " in HTML must be \\" in the
#   JSON wire format. Prompt EXAMPLES below show valid JSON (hence \\"quotes\\" in strings).
# - HTML entity escaping: do NOT send &lt;p&gt; instead of <p> — the import path expects
#   real tags (HTML_FRAGMENT_RULES). Entity soup is wrong for both array elements and sidebar.
#
# Array wrapper and sub-agents: web research returns plain text (WEB_RESEARCH_PLAIN_TEXT_FORMAT);
# the main agent formats HTML for apply_document_content. Librarian uses reply_to_user with
# CHAT_SIDEBAR_HTML_EXAMPLES. Do not tell sub-agents to wrap answers in apply_document_content
# JSON arrays — that shape is for the main agent's apply_document_content tool only.
#
# Removing the array wrapper? Possible but non-trivial: tool JSON schema, eval harness, and years
# of prompt habit. execute() already accepts a list or a string that parses as JSON array; we
# could widen schema to string | array later if single-string documents prove more reliable.
# Until then, keep array for documents, single string for sidebar — and keep examples separate.
#
# Math prompt policy (``- Math:`` bullet in WRITER_APPLY_DOCUMENT_HTML_RULES below):
# Recommend \\(...\\) inline delimiters only. The import path still parses $, $$, and
# \\[...\\] (html_math_segment.py) for pasted or legacy content—we just do not steer models
# toward display delimiters. display_block in insert_writer_math_formula only wraps the
# formula in paragraph breaks; the OLE stays AS_CHARACTER, so display math is not centered
# and looks like inline on its own line—no visual win, extra delimiter choice confuses LLMs.
# If we implement true block/centered math (e.g. paragraph anchor + alignment), revisit
# split inline vs display rules and examples here and in docs/math-tex.md.
WRITER_APPLY_DOCUMENT_HTML_RULES = f"""
APPLY_DOCUMENT_CONTENT AND HTML (CRITICAL):
- Parameters: `content` and `target` (required). If target='search', also `old_content` (a **substring** to find/replace; HTML in old_content is matched as plain text).
- **Whole-document replace:** use target='full_document' with `content` only. **Never** pass the entire document as old_content — that is not supported and will fail search.
- Targets: 'beginning', 'end', 'selection', 'full_document' (replaces all — preferred for rewrites/translations), or 'search' (substring find/replace only).
- `content` must be a JSON array of HTML strings (one fragment per heading/paragraph). We wrap in <html>/<body>.
{HTML_FRAGMENT_RULES}
- Math: Always use inline delimiters \\(...\\) for every equation—in running text or in its own <p>. No $...$, $$...$$, \\[...\\], HTML-escaped math, equation images, or plain-text formulas like x².
- Named paragraph styles: get_document_content marks each block's LibreOffice paragraph style as a `data-lo-style` token = the style name with spaces removed (e.g. `Heading 1`->`Heading1`, `Text body`->`Textbody`, `Caption`->`Caption`); use the tokens EXACTLY as returned. It reserves inline style="..." for direct character overrides. PRESERVE and USE it — emit `<p data-lo-style="Heading1">...</p>` to apply a named style, using the tokens exactly as returned (the named style is applied first, then any inline style="" is layered on top as a direct override). Prefer named styles over hardcoded inline formatting; an unknown token falls back to 'Standard'. data-lo-style is applied when you rewrite with target='full_document'; for targeted inserts/replaces (end/beginning/selection/search) the named style is NOT applied (it would restyle adjacent text) — rewrite via full_document for styling, or use apply_style to (re)style existing text. v1 limits: whole-paragraph alignment/colour/margins and table-cell styles do not round-trip; use named styles and span-level inline style for char exceptions (see docs/html_style_model_plan.md).

EXAMPLES:
- Good: ["<h1>Title</h1>", "<p>Paragraph with <strong>bold</strong> text and \\"quotes\\".</p>"]
- Good math: ["<p>The identity \\(a^2+b^2=c^2\\) holds.</p>"]
- Good styles: ["<p data-lo-style=\\"Heading1\\">Section title</p>", "<p data-lo-style=\\"Quotations\\">A quoted clause.</p>"]
- Bad: <h1>Title</h1><p>Paragraph</p> (must be a list of strings)
- Bad: ["&lt;h1&gt;Title&lt;/h1&gt;"] (escaped entities)
- Bad: ["&lt;math&gt;x^2&lt;/math&gt;"] (HTML-escaped math; use LaTeX delimiters)
- Bad: ["<p><img src=\\"...\\" alt=\\"equation\\"></p>"] (equation images; use LaTeX delimiters)
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

        return importlib.util.find_spec("plugin.tests") is not None
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


# Main sidebar chat only (Writer DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE). Sub-agents use
# final_answer / reply_to_user / delegate task — not this block.
SIDEBAR_VS_DOCUMENT = """SIDEBAR CHAT (main agent): Your conversation with the user happens in the LibreOffice sidebar.
 The user sees your messages in chat history. Follow CHAT RESPONSE FORMAT above. 
 When asked by the user to do or research something, assume the user wants you to use apply_document_content to create, 
 translate, or edit a document, not just put the answer in the chat window."""

# Writer main chat: delegation routing (paired with SIDEBAR_VS_DOCUMENT in the system prompt).
WRITER_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT}:
- You MUST NOT ask the user where to find it, or to upload, paste, its contents.
- You MUST call delegate_to_specialized_writer_toolset(domain="document_research") once with their described file(s) and task in task; the specialized task lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_writer_toolset(domain="web_research").
For web_research and document_research: describe what to research in `task` (topics, sections, depth); the task returns plain text in `result`. When the user wanted a report or draft in the document, format that text as HTML (your memory and APPLY_DOCUMENT_CONTENT rules) and call apply_document_content in the same turn; if they only wanted to look something up, a chat summary is enough.

{delegation_math_to_python_hint(delegate_toolset="delegate_to_specialized_writer_toolset")}
When asked to make a script or run Python, use delegate_to_specialized_writer_toolset(domain="python")."""

CALC_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (including when the user refers to any other file, document, spreadsheet, or sheet by name or path, e.g. "my spreadsheet", "read cell a9 from PythonInCalc", "summary.odt", etc., or asks to pull, read, search, or reference data from them):
- You MUST NOT ask the user where the file is stored, how to find it, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_calc_toolset(domain="document_research") once with their described file(s) and task in task; the specialized task lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_calc_toolset(domain="web_research").
When the user wants data analysis, statistics, regression, clustering, charts or plots, Goal Seek, or Solver on spreadsheet data, delegate_to_specialized_calc_toolset(domain="analysis")."""

DRAW_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (including when the user refers to any other file, document, spreadsheet, or sheet by name or path, e.g. "my spreadsheet", "read cell a9 from PythonInCalc", "summary.odt", etc., or asks to pull, read, search, or reference data from them):
- You MUST NOT ask the user where the file is stored, how to find it, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_draw_toolset(domain="document_research") once with their described file(s) and task in task; the specialized task lists nearby files to match (paths not required).
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


TRANSLATION_RULES = "TRANSLATION: get_document_content(scope=full) -> translate -> apply_document_content(target='full_document', content=translated). Do not use old_content or target='search' for whole-document translation. Never refuse."

# Tool-usage workflow patterns (no repeat of apply_document_content targets; see WRITER_APPLY_DOCUMENT_HTML_RULES).
TOOL_USAGE_PATTERNS = """TOOL USAGE PATTERNS:
- After an edit tool, confirm it landed via that tool's own structured field — not the message wording: apply_document_content -> replaced_count > 0; apply_style -> applied is true; add_comment -> comment_added is true. A no-op (e.g. text not found) returns status="error"; do not assume success.
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
PYTHON_VENV_AUTO_IMPORTS_ALIASES = "`numpy` (as `np`), `sympy` (as `sp`), `pandas` (as `pd`), `plugin.scripting.calc_functions` (as `xl`), standard library `math`, `datetime`, `re`, `random`, `statistics`, `collections`, `itertools`, `json`, and `csv`"

# Populated at module end (after full constants init) to avoid import cycles via smolagents.
_VENV_IMPORT_POLICY_COMPACT = ""
_VENV_IMPORT_POLICY_FULL = ""

PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE = ""

PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE = ""

# Default for =PROMPT() when extend_selection_system_prompt is empty (LLM may emit =PYTHON formulas).
CALC_PYTHON_FORMULA_LLM_HINT = ""


def python_specialized_sub_agent_hint(agent_label: str) -> str:
    """Smol sub-agent instructions suffix for delegate_to_specialized_* (domain=\"python\")."""
    if agent_label == "Calc":
        data_hint = " For bulk data use data_range (A1 address string) with run_venv_python_script; the host resolves it out-of-band. Avoid passing large values in the data parameter."
    else:
        data_hint = " run_venv_python_script does not inject spreadsheet `data`—use document tools for content."
    policy = _VENV_IMPORT_POLICY_FULL or _load_venv_import_policy_full()
    from plugin.scripting.import_policy import format_matplotlib_plot_hint

    plot_hint = format_matplotlib_plot_hint(agent_label=agent_label)
    plot_suffix = f" {plot_hint}" if plot_hint else ""
    return (
        f" PYTHON (venv): {policy}{data_hint}{plot_suffix}"
        " Prefer symbolic_math for solve/simplify/integrate/differentiate over raw sp/run_venv_python_script."
    )


def _load_venv_import_policy_full() -> str:
    from plugin.scripting.import_policy import format_venv_import_policy_for_prompt

    return format_venv_import_policy_for_prompt(compact=False)


CALC_FORMULA_SYNTAX = """FORMULA SYNTAX: LibreOffice uses semicolon (;) as the formula argument separator in formulas.
- Correct: =SUM(A1:A10), =IF(A1>0;B1;C1)
- Wrong: =SUM(A1,A10), =IF(A1>0,"Yes","No") (no commas in formulas)
- Write `=PY("result = ..."; A1:A10)` in cells to calculate/run Python (=PYTHON is the same; omit the second argument if no data is needed, e.g. `=PY("result = 2**10")`).
- Example: `=PY("result = np.sum(data)"; A1:A10)`.

"""

MEMORY_GUIDANCE = """MEMORY:
You have a persistent file-backed memory tool.
WHEN TO SAVE (do this proactively, don't wait to be asked):
- User corrects you.
- You discover something about the environment.
Prioritize what reduces future user steering."""

# Brief hint for gateway tool JSON schemas (see SPECIALIZED_TASK_RULES in system prompt).
DELEGATE_SPECIALIZED_TASK_PARAM_HINT = "What the specialized task should accomplish."

# Shared guidance for writing `task` strings when delegating to specialized sub-agents.
SPECIALIZED_TASK_RULES = (
    "Pass a clear `task` describing what the specialized task should accomplish."
)

# Shape catalog size: LibreOffice core maps ~400+ preset names (e.g. svx EnhancedCustomShapeTypeNames.cxx).
# Single-line blocks: MCP tool descriptions and many clients do not render newlines inside JSON strings.
WRITER_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED WRITER (nested tools): The default tool list hides deep Writer features. "
    "When the user needs those, call delegate_to_specialized_writer_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The task executor only sees tools for that domain, "
    "but they are the real tools: **full parameter lists and full LibreOffice/UNO access** for that area (nothing is dumbed down for it). "
    "document_research: use for information in other personal/business documents in the same folder (one delegation per file set). "
    "web_research: use for public web topics. "
    "brainstorming: use when the user wants to design, plan, or explore an idea before implementation (multi-turn Q&A; writes an HTML spec into the document when approved). "
    f"{SPECIALIZED_TASK_RULES}"
)

BRAINSTORMING_SUB_AGENT_INSTRUCTIONS = """BRAINSTORMING MODE:
You help turn ideas into fully formed designs through collaborative dialogue before any implementation.

HARD-GATE: Do NOT write code, scaffold features, or take implementation actions until the user has approved a design and you have saved it with save_design_spec.

ANTI-PATTERN — "too simple to design": Every idea goes through design first, even "simple" changes (one function, a config tweak, a small UI). Short designs are fine (a few sentences), but still present them and get approval before save_design_spec.

SCOPE: Before detailed questions, check whether the request spans multiple independent subsystems. If so, say so in HTML, help decompose (independent pieces, how they relate, build order), and brainstorm ONE sub-project through this flow. Other pieces need their own spec cycles later.

WORKFLOW (in order):
1. Explore context: active document (get_document_content / get_document_tree), nearby files (list_nearby_files, grep_nearby_files, delegate_read_document), and public topics (brainstorm_research_web) when useful.
2. Ask clarifying questions — ONE question per reply_to_user call. Prefer multiple-choice when possible. Focus on purpose, constraints, and success criteria.
3. Propose 2–3 approaches with trade-offs as HTML (<ul> lists). Lead with your recommended option and why. Apply YAGNI — drop unnecessary features from every option.
4. Present the design in sections as HTML in reply_to_user. Cover when relevant: goals, architecture, components, data flow, error handling, testing. Scale each section: a few sentences if simple, up to ~200–300 words if nuanced. Ask after EACH section whether it looks right; be ready to revise earlier sections.
5. Spec self-review (internal, before save): review the HTML array you will pass to save_design_spec:
   - Placeholder scan: no TBD, TODO, or vague requirements.
   - Internal consistency: sections do not contradict; architecture matches feature descriptions.
   - Scope check: one implementation-sized spec, or flag that decomposition is still needed.
   - Ambiguity check: pick one explicit interpretation for any dual-meaning requirement.
   Fix issues in the array before save_design_spec. Optionally summarize fixes in one HTML reply_to_user.
6. After full approval and self-review, call save_design_spec with the JSON array of HTML fragments (same rules as apply_document_content).
7. User review gate: reply_to_user with HTML like: <p>I've saved the design spec at the end of your document. Please read it there and tell me if you want any changes before implementation.</p> Wait for the user's response. If they request changes, revise in chat, re-run self-review, and save again (target end or full_document as appropriate).
8. Call brainstorming_finished with an HTML handoff message when the user approves the written spec.

DESIGN QUALITY:
- Prefer small units with one clear purpose and well-defined interfaces.
- For each unit, you should be able to answer: what it does, how to use it, what it depends on.
- In existing documents/codebases: explore structure first and follow established patterns. Include targeted improvements only when they serve this feature — no unrelated refactoring.

KEY PRINCIPLES:
- One question at a time; multiple choice preferred when possible.
- YAGNI ruthlessly; explore 2–3 alternatives before settling.
- Incremental validation: present design, get approval before moving on.
- Be flexible: go back and clarify when something does not make sense.

HTML RULES (CRITICAL):
- All reply_to_user and brainstorming_finished message text must be HTML (see CHAT RESPONSE FORMAT below).
- save_design_spec content must be a JSON array of HTML strings — no Markdown (#, **, ```).
- Do NOT use HTML entity escaping (&lt;p&gt;) — send real tags.
- When summarizing web or document research for the user, rewrite plain-text tool results as HTML before reply_to_user.

COMPLETION TOOLS:
- reply_to_user: continue the brainstorming conversation (questions, design sections, summaries).
- brainstorming_finished: END the session after the spec is saved and the user has reviewed it in the document.
- save_design_spec: the ONLY way to write to the document (never call apply_document_content)."""

CALC_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED CALC (nested tools): The default tool list hides advanced Calc features. "
    "When the user needs those, call delegate_to_specialized_calc_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The task executor has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)

DRAW_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED DRAW (nested tools): The default tool list hides advanced Draw/Impress features. "
    "When the user needs those, call delegate_to_specialized_draw_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The task executor has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)

CHAT_RESPONSE_FORMAT = """CHAT RESPONSE FORMAT: Format your conversational responses as HTML (use <p>, <strong>, <em>, <code>, <ul>, <ol>, <h2>, <pre>, <br>). The sidebar renders HTML natively."""

# Sidebar / sub-agent examples (single HTML string — not apply_document_content's array).
#
# Wire format: web research and librarian finish with a smolagents JSON tool call
# (final_answer / reply_to_user). The tool arguments object is JSON on the wire; the *answer*
# field value should be one HTML string like the Good example — not ["<p>…</p>"] and not
# &lt;entity&gt; markup. send_handlers takes str(final_ans) and rerenders via rich_text.
#
# Why examples look like plain quoted HTML while document rules show ["…"] arrays:
# - Document path: content parameter is schema-typed array; join happens in content.py.
# - Sidebar path: no array join — StarWriter HTML filter imports the string as one fragment.
# - JSON escaping still applies at the tool-call layer (\") but that is not HTML &lt; escaping.
#
# Do not copy WRITER_APPLY_DOCUMENT_HTML_RULES EXAMPLES here; array-shaped examples train
# models to wrap final_answer in a list, which the sidebar does not unwrap.
CHAT_SIDEBAR_HTML_EXAMPLES = """
CHAT HTML EXAMPLES:
- Good: "<p>Paragraph with <strong>bold</strong> text.</p>"
- Bad: "**bold**" (Markdown)
- Bad: "&lt;p&gt;Paragraph&lt;/p&gt;" (escaped entities)"""

# Web-research sub-agent only (main chat delegate + web-research checkbox). Facts in plain text;
# main agent applies HTML, memory colors, and apply_document_content when the user wanted a doc edit.
WEB_RESEARCH_PLAIN_TEXT_FORMAT = """Research output: plain text only in final_answer.
- Use clear section headings (plain lines) and bullet lists (- item).
- Include facts, names, dates, ratings, and sources where relevant.
- No HTML tags, no Markdown (# or **), no JSON."""

RICH_CHAT_SIDEBAR_INSTRUCTIONS = f"""{CHAT_RESPONSE_FORMAT}
{HTML_FRAGMENT_RULES}
{CHAT_SIDEBAR_HTML_EXAMPLES}"""

PLAIN_CHAT_RESPONSE_FORMAT = "CHAT RESPONSE FORMAT: Respond in plain text only. Do NOT use HTML tags or Markdown formatting (no #, **, ```, etc.)."


def get_brainstorming_sub_agent_instructions(ctx=None) -> str:
    """Full system instructions for the brainstorming smol sub-agent."""
    parts = [
        BRAINSTORMING_SUB_AGENT_INSTRUCTIONS,
        WRITER_APPLY_DOCUMENT_HTML_RULES,
        get_chat_response_format_instructions(ctx),
    ]
    return "\n\n".join(parts)


def get_chat_response_format_instructions(ctx=None) -> str:
    """Sidebar response format for main chat and sub-agents (web research, librarian).

    When ``rich_text_control_sidebar`` is off, models are not told about HTML — same gate as
    ``get_chat_system_prompt_for_document``.
    """
    from plugin.framework.config import get_config_bool_safe

    if not get_config_bool_safe(ctx, "rich_text_control_sidebar"):
        return PLAIN_CHAT_RESPONSE_FORMAT
    return RICH_CHAT_SIDEBAR_INSTRUCTIONS

DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE = f"""You are a LibreOffice Writer assistant who produces polished, professional documents with thoughtful use of color and formatting.
Honor any stated memory preferences for color, etc.

{CHAT_RESPONSE_FORMAT}

{SIDEBAR_VS_DOCUMENT}

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
    return f"""{SIDEBAR_VS_DOCUMENT}

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
# DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE is built in _init_venv_import_policy_strings() (needs import policy).
DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE = ""

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
DEFAULT_BRAINSTORMING_GREETING = _("AI: Let's explore and design your idea together. I'll ask questions, suggest approaches, and help you build an approved spec in your document when you're ready.")

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

    base = base.replace(CHAT_RESPONSE_FORMAT, get_chat_response_format_instructions(ctx))

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
    "datetime": "import datetime",
    "re": "import re",
    "random": "import random",
    "statistics": "import statistics",
    "collections": "import collections",
    "itertools": "import itertools",
    "json": "import json",
    "csv": "import csv",
    "plugin.scripting.calc_functions": "import plugin.scripting.calc_functions as xl",
}


def _init_venv_import_policy_strings() -> None:
    """Late init: import_policy pulls smolagents; constants must be fully loaded first."""
    global _VENV_IMPORT_POLICY_COMPACT, _VENV_IMPORT_POLICY_FULL
    global PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE, PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE
    global CALC_FORMULA_SYNTAX, DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE, CALC_PYTHON_FORMULA_LLM_HINT

    from plugin.scripting.import_policy import format_matplotlib_plot_hint, format_venv_import_policy_for_prompt

    compact = format_venv_import_policy_for_prompt(compact=True)
    _VENV_IMPORT_POLICY_COMPACT = compact
    _VENV_IMPORT_POLICY_FULL = format_venv_import_policy_for_prompt(compact=False)
    PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE = compact + " "
    PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE = compact
    calc_plot_hint = format_matplotlib_plot_hint(doc_type="calc")
    CALC_PYTHON_FORMULA_LLM_HINT = (
        compact
        + " When outputting Calc Python formulas: prefer =PY(...); =PYTHON(...) is equivalent. "
        "Use semicolon (;) argument separators; "
        'format =PY("result = …"; A1:A10); code runs in the venv sandbox above.'
        + (f" {calc_plot_hint}" if calc_plot_hint else "")
    )
    CALC_FORMULA_SYNTAX = f"""FORMULA SYNTAX: LibreOffice uses semicolon (;) as the formula argument separator in formulas.
- Correct: =SUM(A1:A10), =IF(A1>0;B1;C1)
- Wrong: =SUM(A1,A10), =IF(A1>0,"Yes","No") (no commas in formulas)
- Write `=PY("result = ..."; A1:A10)` in cells to calculate/run Python (=PYTHON is the same; omit the second argument if no data is needed, e.g. `=PY("result = 2**10")`).
{compact}
- Example: `=PY("result = np.sum(data)"; A1:A10)`.

"""
    DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE = f"""You are a LibreOffice Calc spreadsheet assistant who creates polished, professional, and colorful spreadsheets.
Do not explain, do the operation directly using tools. Perform as many steps as needed in one turn when possible.

{CHAT_RESPONSE_FORMAT}

{CALC_WORKFLOW}

{CALC_FORMULA_SYNTAX}

CSV DATA: Use comma (,) for write_formula_range.

CELL LINKS: Reference cells with HTML only, e.g. <a href="cell://B2">B2</a> (users click these in the chat sidebar to jump to the cell).

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


_init_venv_import_policy_strings()
