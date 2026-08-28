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

"""Main-chat system prompts for Writer, Calc, and Draw.

Layout: Generic, Writer, Calc, Draw, then Assembly (dispatch + late init).

Other important prompts (not assembled here):
- plugin/chatbot/brainstorming.py, writing.py, deep_research_session.py, web_research.py, librarian.py, ppt_master.py
- plugin/chatbot/panel_factory.py (sidebar-mode greetings)
- plugin/calc/prompt_function.py (=PROMPT() cell)
- plugin/framework/client/response_normalizers.py (dev-build LLM prefix)
- plugin/contrib/smolagents/toolcalling_agent_prompts.py (smol ToolCallingAgent)
- scripts/prompt_optimization/ (Writer eval harness)
"""

# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Delegation primitives (inputs to core directives and tool schemas)
# ---------------------------------------------------------------------------

# Research routing (short); domain bullets use these strings as-is.
DELEGATION_USER_FILE_DATA_HINT = "to use information that is not in the current document, and may be in (my / our) personal or business documents"
DELEGATION_PUBLIC_WEB_HINT = "to research public topics"

# Main agent only: after research delegates return plain text, write HTML to the document (not sidebar).
RESEARCH_DELEGATE_TO_DOCUMENT = (
    "After web_research or document_research, you MUST call apply_document_content so the user can see and edit the report (empty doc → target='beginning').\n"
    "Sidebar: brief confirmation only — NEVER paste the full report in chat unless the user explicitly asked chat-only."
)
APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT = "Required after web_research or document_research delegates return."

# Canonical wording for tools that return a para_index / paragraph_index to the model. Those indexes
# are internal addressing only; the user never sees them and they shift as the document changes, so
# the model must refer to a place by quoting its text, not by number. Append to such tool
# descriptions so the rule is stated uniformly wherever an index is exposed (#1).
PARAGRAPH_INDEX_DIRECTIVE = (
    "para_index / paragraph_index values are INTERNAL addressing only — NEVER cite paragraph numbers "
    "to the user (they don't see them and they shift as the document changes); to point the user at a "
    "place, quote the first few words of its text instead (e.g. \"the sentence starting 'The Amazon…'\")."
)


def delegation_math_to_python_hint(*, delegate_toolset: str) -> str:
    """Writer/Draw: route computational math to the python specialized sub-agent (fast local venv)."""
    return (
        "For computational or numeric math (exact values, primes, statistics, symbolic algebra, or non-trivial calculation), "
        f'do not answer from memory—use {delegate_toolset}(domain="python") for fast local numeric computation.'
    )


# Brief hint for gateway tool JSON schemas (see SPECIALIZED_TASK_RULES in system prompt).
DELEGATE_SPECIALIZED_TASK_PARAM_HINT = "What the specialized task should accomplish."

# Shared guidance for writing `task` strings when delegating to specialized sub-agents.
SPECIALIZED_TASK_RULES = (
    "Pass a clear `task` describing what the specialized task should accomplish."
)


# ---------------------------------------------------------------------------
# Shared HTML primitives (sidebar + document fragments)
# ---------------------------------------------------------------------------

# Tag-level rules shared by sidebar chat and apply_document_content fragments.
# Container differs: single HTML string (sidebar) vs JSON array (document) — docs/chat/sidebar-implementation.md § Chat prompt constants.
HTML_FRAGMENT_RULES = """
- Use <br> for line breaks within an element; <p> for paragraphs.
- Raw Unicode (é, ü, ©); straight double quotes ("), not curly/smart quotes or HTML entities. Send <h1> not &lt;h1&gt;. Preserve intentional spacing.
- Do NOT use Markdown (#, **, ```, etc.)."""

# Sidebar / sub-agent examples (single HTML string — not apply_document_content's array). See docs/chat/sidebar-implementation.md § Chat prompt constants.
CHAT_SIDEBAR_HTML_EXAMPLES = """
CHAT HTML EXAMPLES:
- Good: "<p>Paragraph with <strong>bold</strong> text.</p>"
- Bad: "**bold**" (Markdown)
- Bad: "&lt;p&gt;Paragraph&lt;/p&gt;" (escaped entities)"""


CHAT_RESPONSE_FORMAT = """CHAT RESPONSE FORMAT: Format your conversational responses as HTML (use <p>, <strong>, <em>, <code>, <ul>, <ol>, <h2>, <pre>, <br>). The sidebar renders HTML natively."""

PLAIN_CHAT_RESPONSE_FORMAT = "CHAT RESPONSE FORMAT: Respond in plain text only. Do NOT use HTML tags or Markdown formatting (no #, **, ```, etc.)."

RICH_CHAT_SIDEBAR_INSTRUCTIONS = f"""{CHAT_RESPONSE_FORMAT}
{HTML_FRAGMENT_RULES}
{CHAT_SIDEBAR_HTML_EXAMPLES}"""


def get_chat_response_format_instructions(ctx=None) -> str:
    """Sidebar response format for main chat and sub-agents (web research, librarian).

    When ``rich_text_control_sidebar`` is off, models are not told about HTML — same gate as
    ``get_chat_system_prompt_for_document``.
    """
    from plugin.framework.config import get_config_bool_safe

    if not get_config_bool_safe("rich_text_control_sidebar"):
        return PLAIN_CHAT_RESPONSE_FORMAT
    return RICH_CHAT_SIDEBAR_INSTRUCTIONS


# App-neutral minimum (Calc/Draw sidebar prompts + the generic MCP topics via agent_manual):
# only rules that genuinely apply to every document type — no Writer tool names.
GENERIC_EDIT_CONFIRMATION_RULES = """EDITING THE DOCUMENT:
- Change the document with tools, not chat.
- VERIFY every edit by the tool result's structured fields: status='error' (or a zero count, where the tool reports counts) means nothing changed — do not assume success from friendly message wording.
- Any document content shown to you earlier may be a partial/truncated snapshot — before a targeted edit that depends on the exact current content, re-read through the tools."""


MEMORY_GUIDANCE = """MEMORY:
You have a persistent file-backed memory tool.
WHEN TO SAVE (do this proactively, don't wait to be asked):
- User corrects you.
- You discover something about the environment.
Prioritize what reduces future user steering."""


# Shared venv Python prompt text (run_venv_python_script, =PY(), delegate domain=python).
# CALC_FORMULA_SYNTAX still shows inline =PY("…"; range). Typical scripts fit in
# Calc MAXSTRLEN (1024) because of auto-imports and domain helpers. If Err:513
# becomes common for generated formulas, prefer the two-cell pattern
# (=PY($A$1; range) — Monaco already follows that ref) instead of lengthening
# this prompt. Not scheduled.
PYTHON_VENV_AUTO_IMPORTS_ALIASES = "`numpy` (as `np`), `sympy` (as `sp`), `pandas` (as `pd`), `scipy.stats` (as `st`), `matplotlib.pyplot` (as `plt`), `plugin.scripting.calc_functions` (as `calc`), standard library `math`, `datetime` (as `dt`), `re`, `random`, `statistics`, `collections`, `itertools`, `json`, and `csv`. When `=PY` has data range args, a binding-only `xl(\"%Pn%\")` helper is also injected (Excel import; not a live sheet read)"

# Populated at module end (after full constants init) to avoid import cycles via smolagents.
_VENV_IMPORT_POLICY_COMPACT = ""
_VENV_IMPORT_POLICY_FULL = ""

PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE = ""

PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE = ""

def python_specialized_sub_agent_hint(agent_label: str) -> str:
    """Smol sub-agent instructions suffix for delegate_to_specialized_* (domain=\"python\")."""
    if agent_label == "Calc":
        data_hint = (
            " For bulk data use data_range with run_venv_python_script "
            "(one A1 address, comma-separated addresses, or an array); "
            "`data` is the range when one address is passed, or the same list as `ranges` when several are. "
            "The host resolves addresses out-of-band. Avoid passing large values in the data parameter."
        )
    else:
        data_hint = " run_venv_python_script does not inject spreadsheet `data`—use document tools for content."
    _ensure_venv_import_policy_strings()
    policy = _VENV_IMPORT_POLICY_FULL or _load_venv_import_policy_full()
    from plugin.scripting.import_policy import format_matplotlib_plot_hint, format_units_helper_hint

    plot_hint = format_matplotlib_plot_hint(agent_label=agent_label)
    plot_suffix = f" {plot_hint}" if plot_hint else ""
    units_hint = format_units_helper_hint()
    return (
        f" PYTHON (venv): {policy}{data_hint}{plot_suffix}"
        " Prefer symbolic_math for solve/simplify/integrate/differentiate over raw sp/run_venv_python_script."
        f" {units_hint}"
    )


def _load_venv_import_policy_full() -> str:
    from plugin.scripting.import_policy import format_venv_import_policy_for_prompt

    return format_venv_import_policy_for_prompt(compact=False)

# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

WRITER_CHAT_PERSONA = """You are a LibreOffice Writer assistant who produces polished, professional documents with thoughtful use of color and formatting.
Honor any stated memory preferences for color, etc."""


# Main sidebar chat only (Writer DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE). Sub-agents use
# final_answer / reply_to_user / delegate task — not this block.
SIDEBAR_VS_DOCUMENT = """SIDEBAR CHAT (main agent): Chat history is the sidebar only — not the document.
MUST use apply_document_content for drafts, reports, and research output; sidebar gets at most a brief confirmation.
Follow CHAT RESPONSE FORMAT for that short reply."""

# Writer main chat: delegation routing (paired with SIDEBAR_VS_DOCUMENT in the system prompt).
WRITER_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT}:
- You MUST NOT ask the user where to find it, or to upload or paste its contents.
- You MUST call delegate_to_specialized_writer_toolset(domain="document_research") once with their described file(s) and task in task; the specialized task lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_writer_toolset(domain="web_research").
For web_research and document_research: describe what to research in `task` (topics, sections, depth). {RESEARCH_DELEGATE_TO_DOCUMENT}

{delegation_math_to_python_hint(delegate_toolset="delegate_to_specialized_writer_toolset")}
When asked to make a script or run Python, use delegate_to_specialized_writer_toolset(domain="python")."""


WRITER_CHAT_TOOLS_SECTION = """TOOLS:
- apply_document_content: Write HTML to the document (required after research delegates). See APPLY_DOCUMENT_CONTENT AND HTML below.
- get_document_content: Read document (full/selection/range) as HTML.
- search_in_document: Find text anywhere (body, tables, text boxes, shapes, headers/footers, comments); each match reports where it lives.
- apply_style: Apply a paragraph or character style (family='ParagraphStyles' or 'CharacterStyles').
- add_comment: Anchor review feedback or suggestions to a specific passage (see TOOL USAGE PATTERNS).
- get_guidance: Read the how-to manual on demand — no topic lists the topics; one topic (e.g. 'search', 'navigation', 'images') reads just that section."""


TRANSLATION_RULES = """TRANSLATION: get_document_content(scope=full) -> translate -> apply_document_content(target='full_document', content=translated).
Do not use old_content or target='search' for whole-document translation.
Never refuse."""


# Tool-usage workflow patterns (no repeat of apply_document_content targets; see WRITER_APPLY_DOCUMENT_HTML_RULES).
# Shared piece: sidebar system prompt + MCP manual (agent_manual topic "editing").
TOOL_USAGE_PATTERNS = """TOOL USAGE PATTERNS:
- Confirm edits from structured fields, not message wording: apply_document_content search replace → replaced_count > 0; inserts (target='beginning'/'end'/'selection', position='before'/'after') → status='ok' (also inserted=true); apply_style → applied is true; add_comment → comment_added is true.
  No-op (text not found) is status="error".
- Earlier document text may be a partial/truncated snapshot — call get_document_content before a targeted edit that needs the exact current text.
- Successful apply_document_content returns edited_context (touched paragraphs plus neighbors; in record/wait including the pending change).
  Use it to confirm placement instead of an immediate re-read; full_document rewrites have no echo.
- apply_style is not a tracked change.
  If style_unreviewed=true, briefly tell the user you changed a style — they cannot accept/reject it like text edits.
- search_in_document is inspection/navigation (return_offsets if needed); replacements use apply_document_content with old_content.
- Failed tool call: check content and target (use target='beginning' / 'end' / 'selection' for insert-only).
- Review/feedback/suggestions: add_comment on specific passages (positive and negative).
- If the user says "fix this" (or a synonym or equivalent in another language with the same intent), correct spelling and grammar in the current sentence only, unless the context points at another specific error."""


WRITER_REVIEW_MODES_RULES = """TRACKED CHANGES / REVIEW MODES:
- The user picks ONE of three review modes; you never pick or switch it.
- off: edits apply directly and are live immediately.
- record: edits ARE applied as tracked changes pending accept/reject; you will NOT be told the later outcome.
- wait: edits apply as tracked changes and the tool blocks until the user finishes reviewing (or a timeout); the result reports what was accepted or rejected.
- apply_document_content's RESULT carries that call's review state (record: review_mode / pending_review; wait: a review field with the outcome) — trust the latest result, since the user can switch modes mid-session.
- In record and wait, NEVER accept or reject changes yourself (no manage_tracked_changes accept/reject) — resolving redlines is the user's decision.
- When reading, get_document_content lists pending changes under tracked_changes — they are pending review, not errors to fix."""


# apply_document_content only — design notes in docs/chat/sidebar-implementation.md § Chat prompt constants and docs/writer/math-tex.md.
WRITER_APPLY_DOCUMENT_HTML_RULES = f"""APPLY_DOCUMENT_CONTENT AND HTML (CRITICAL):
- Required: `content` and `target`.
  Targets: 'beginning', 'end', 'selection', 'full_document' (preferred for rewrites/translations), 'search' (substring find/replace; also `old_content` as a **substring** — HTML in old_content is matched as plain text).
- **Never** pass the entire document as old_content — that is not supported and will fail search.
- target='search': old_content may span paragraphs, but each interior line must match a WHOLE paragraph.
  position='before'/'after' INSERTS next to the match and leaves it untouched — add a paragraph without re-sending the clause.
- Reach: body, table cells, text frames.
  Floating drawing-shape text: in place only when review is off — in record/wait it cannot become a tracked change, so the tool routes you to the shapes domain.
  Rich/block HTML in a table cell is not supported (clear error, document untouched); use plain text or inline tags.
- `content` is a JSON array of HTML strings (one fragment per heading/paragraph).
  We wrap in <html>/<body>.
{HTML_FRAGMENT_RULES}
- Math (CRITICAL): Format mathematical equations, formulas, fractions, integrals, matrices, and non-trivial calculations in LaTeX using \\(...\\) for inline math or \\[\u2026\\] for centered display math (e.g. \\(a^2+b^2=c^2\\) or \\(\\frac{{a}}{{b}}\\)).
  Use standard HTML <sup> and <sub> tags for simple text superscripts and subscripts (e.g. 10<sup>th</sup>, H<sub>2</sub>O, x<sup>2</sup>) to preserve native character formatting without creating OLE Math objects.
  Do NOT write equations in plain text (avoid plain a/b, etc.).
  No $, $$, HTML-escaped math, or equation images.
- Named styles: get_document_content marks each block `data-lo-style` = style name with spaces removed (`Heading 1`→`Heading1`).
  Copy tokens exactly. Prefer named styles; unknown token → Standard.
  inline style="" is a character override on top of the named style.
  data-lo-style applies only on target='full_document' — on 'beginning'/'end'/'selection'/'search' it is ignored because it would restyle adjacent text (use apply_style or a full_document rewrite).
  v1: whole-paragraph alignment/colour/margins and table-cell styles do not round-trip.

EXAMPLES:
- Good: ["<h1>Title</h1>", "<p>Paragraph with <strong>bold</strong> text and \\"quotes\\".</p>"]
- Good math: ["<p>The identity \\(a^2+b^2=c^2\\) holds.</p>", "\\[E = mc^2\\]", "<p>Water molecule: H<sub>2</sub>O, 10<sup>th</sup> edition.</p>"]
- Good styles: ["<p data-lo-style=\\"Heading1\\">Section title</p>", "<p data-lo-style=\\"Quotations\\">A quoted clause.</p>"]
- Bad: <h1>Title</h1><p>Paragraph</p> (must be a list of strings)"""


# Single-line blocks: MCP tool descriptions and many clients do not render newlines inside JSON strings.
WRITER_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED WRITER (nested tools): The default tool list hides deep Writer features. "
    "When the user needs those, call delegate_to_specialized_writer_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The executor has the real tools for that domain. "
    "document_research: other personal/business files in the same folder (one delegation per file set). "
    "web_research: public web topics; main agent writes returned report to document (apply_document_content). "
    f"{SPECIALIZED_TASK_RULES}"
)


WRITER_SEARCH_RULES = """SEARCH:
- search_in_document finds text ANYWHERE — body paragraphs and headings, table cells, text boxes/frames, floating drawing shapes, page headers/footers, and comments.
- Each match reports WHERE it lives (e.g. "body", "table 'X' cell B2", "text box 'Y'", "shape 'Z'", "header (page style 'Standard')", "comment by 'A'") plus the surrounding text; use return_offsets=true for character ranges.
- When pointing the user to a match, quote the first words of its text and its location — never an internal paragraph index."""

WRITER_NAVIGATION_RULES = """NAVIGATING LARGE DOCUMENTS (map first, then drill — don't dump):
- get_document_tree(content_strategy='heading_only') gives the heading outline plus stats and stable _mcp_ bookmark ids.
- nav_heading_children (structural domain; locator='bookmark:_mcp_…' or 'heading:1.2') reads one section on demand.
- search_in_document jumps to specific text.
- Reserve get_document_content(scope='full') for short documents or a deliberate full read."""

WRITER_IMAGES_RULES = """IMAGES:
- Image tools live in the 'images' domain: image_insert, image_delete, image_replace, image_list, image_get_info (includes crop_mm), image_download.
  OCR (extract_text_from_image) lives in the 'vision' domain.
- image_set_properties resizes (width_mm/height_mm), repositions (hori_orient/vert_orient — friendly values like left/center/right/top/bottom work), and crops (crop_top_mm / crop_bottom_mm / crop_left_mm / crop_right_mm — mm trimmed per edge).
- To actually SEE an image (vision-capable models), call get_image — by graphic name, selection=true, or page=N to render that whole page.
  For a bulk read with pictures embedded, pass include_images=true to get_document_content."""


# Writer sidebar modes — not exposed on delegate_to_specialized_writer_toolset (user picks from dropdown).
WRITER_SIDEBAR_ONLY_DOMAINS = frozenset({"brainstorming", "writing_plan", "deep_research"})


def _build_writer_chat_system_prompt_template() -> str:
    """Assemble Writer main-chat system prompt in model-facing order.

    HYBRID delivery of the shared pieces: the ambient prompt carries the original pieces plus
    the safety-critical review-modes piece (a model must know it may not resolve its own
    tracked changes BEFORE it acts — weaker models never ask first); the reference pieces
    (search, navigation, images) are pulled on demand through the get_guidance tool, so every
    turn stays lean. The MCP-only extras (e.g. the HTTP 429 concurrency contract) stay out of
    this ambient prompt — the sidebar runs in-process; if a sidebar model pulls the concurrency
    topic anyway it just reads an inert rule."""
    return "\n\n".join([
        WRITER_CHAT_PERSONA,
        CHAT_RESPONSE_FORMAT,
        SIDEBAR_VS_DOCUMENT,
        "{core_directives}",
        WRITER_CHAT_TOOLS_SECTION,
        TRANSLATION_RULES,
        TOOL_USAGE_PATTERNS,
        WRITER_REVIEW_MODES_RULES,
        WRITER_APPLY_DOCUMENT_HTML_RULES,
        "{specialized_delegation}",
        MEMORY_GUIDANCE,
    ])


DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE = _build_writer_chat_system_prompt_template()


DEFAULT_WRITER_GREETING = "AI: I can edit or translate your document instantly with professional formatting and color. Try me!"

# ---------------------------------------------------------------------------
# Calc
# ---------------------------------------------------------------------------

# : str so checkers keep this as str (Writer/Draw already are, via a str-returning call).
CALC_CORE_DIRECTIVES: str = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (another file/sheet by name or path, e.g. "my spreadsheet", "cell A9 from PythonInCalc"):
- You MUST NOT ask the user where the file is stored, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_calc_toolset(domain="document_research") once with their described file(s) and task in task; nearby files are matched (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_calc_toolset(domain="web_research").
Python on sheet data: write_formula_range of =PY (that tool's description)."""


CALC_WORKFLOW = """WORKFLOW:
1. get_sheet_summary for size/headers.
   read_cell_range only for a small peek (headers or a few dozen cells).
   A large range in chat overloads the model context — for transforms, pass the A1 address to =PY instead of reading the values.
2. Do the work with tools. Use ranges, not one cell at a time.
3. Short confirmation; if you changed cells, name the range (e.g. "Wrote totals in B5:B8")."""


# Parked from Calc chat/MCP domain lists. Compute in Calc chat is =PY() on write_formula_range.
CALC_HIDDEN_SPECIALIZED_DOMAINS = frozenset({"analysis", "python"})


CALC_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED CALC (nested tools): The default tool list hides advanced Calc features. "
    "When the user needs those, call delegate_to_specialized_calc_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The task executor has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)


# Alias of CALC_FORMULA_SYNTAX after late init (spreadsheet-import Phase 6). Not the =PROMPT() default.
CALC_PYTHON_FORMULA_LLM_HINT = ""

# Builtin sum/min/max iterate rows of the 2D CalcRange, not cells (column A1:A3 is [[10],[20],[30]]).
CALC_PYTHON_DATA_SHAPE_LLM_HINT = (
    "`data` is always 2D (column A1:A3 is [[10],[20],[30]]); use np.sum(data) / np.mean(data), "
    "not builtin sum/min/max (those iterate rows → TypeError int+list)."
)


# Built in _init_venv_import_policy_strings() (needs import policy).
CALC_FORMULA_SYNTAX = ""

# DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE is built in _init_venv_import_policy_strings() (needs import policy).
DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE = ""


DEFAULT_CALC_GREETING = "AI: I can help you with formulas, data analysis, and colorful charts. Try me!"

def _build_calc_chat_system_prompt_template() -> str:
    """Assemble Calc main-chat system prompt (needs CALC_FORMULA_SYNTAX from late init)."""
    _ensure_venv_import_policy_strings()
    return f"""You are a LibreOffice Calc spreadsheet assistant who creates polished, professional, and colorful spreadsheets.
Do not explain, do the operation directly using tools. Perform as many steps as needed in one turn when possible.

{CHAT_RESPONSE_FORMAT}

{GENERIC_EDIT_CONFIRMATION_RULES}

{CALC_WORKFLOW}

{CALC_FORMULA_SYNTAX}

CSV DATA: Use comma (,) for write_formula_range.

CELL LINKS: Reference cells with HTML only, e.g. <a href="cell://B2">B2</a> (users click these in the chat sidebar to jump to the cell).
Other sheets use the same Calc dot as formulas: <a href="cell://Orders.A1">Orders.A1</a>.

TOOLS: read_cell_range, get_sheet_summary, write_formula_range, set_style, insert_cell_html, merge_cells, delete_structure (see each tool).
set_style: fixed properties only — not mixed rich text in a cell; use insert_cell_html for that.

{{specialized_delegation}}

{{core_directives}}"""

# ---------------------------------------------------------------------------
# Draw
# ---------------------------------------------------------------------------

DRAW_CORE_DIRECTIVES = f"""When the user wants {DELEGATION_USER_FILE_DATA_HINT} (including when the user refers to any other file, document, spreadsheet, or sheet by name or path, e.g. "my spreadsheet", "read cell a9 from PythonInCalc", "summary.odt", etc., or asks to pull, read, search, or reference data from them):
- You MUST NOT ask the user where the file is stored, how to find it, or to upload, paste, or share its contents.
- You MUST call delegate_to_specialized_draw_toolset(domain="document_research") once with their described file(s) and task in task; the specialized task lists nearby files to match (paths not required).
When the user wants {DELEGATION_PUBLIC_WEB_HINT}, delegate_to_specialized_draw_toolset(domain="web_research").

{delegation_math_to_python_hint(delegate_toolset="delegate_to_specialized_draw_toolset")}
When asked to make a script or run Python, use delegate_to_specialized_draw_toolset(domain="python")."""


# Impress/Draw sidebar modes — PPT-Master combo box; hidden from main chat and draw delegate.
IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS = frozenset({"ppt-master"})


DRAW_SPECIALIZED_DELEGATION_TEMPLATE = (
    "SPECIALIZED DRAW (nested tools): The default tool list hides advanced Draw/Impress features. "
    "When the user needs those, call delegate_to_specialized_draw_toolset with: domain one of: {domains} "
    "and a `task` string that fully specifies what the specialized task must do. The task executor has full tool access for that domain. "
    f"{SPECIALIZED_TASK_RULES}"
)


DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE = """You are a LibreOffice Draw/Impress assistant who creates polished, professional, and colorful visual content.
Do not explain - do the operation directly using tools. Perform as many steps as needed in one turn when possible.

""" + CHAT_RESPONSE_FORMAT + """

""" + GENERIC_EDIT_CONFIRMATION_RULES + """

WORKFLOW:
1. Understand the user's request.
2. If needed, use list_pages, read_slide_text, or get_presentation_info to understand current slides and layout.
3. Use the specialized delegation tool to perform shape operations (create, edit, group, etc.), transitions, masters, notes, or charts.
4. Give a short confirmation; when you changed pages/shapes, mention them.

TOOLS (grouped by use):

READ:
- list_pages: List pages/slides in the document.
- read_slide_text: Extract text content and speaker notes from a slide.
- get_presentation_info: Slide count, dimensions, master slide names, and Impress status.
- get_draw_tree: Semantic tree (DOM) of shapes, layout, and hierarchy on a page.
- list_placeholders: List text placeholders (title, subtitle, body) on a slide (Impress).
- get_placeholder_text: Get text from a slide placeholder by role or index.

WRITE:
- add_slide: Insert a new slide (page) at specified index.
- delete_slide: Remove a slide (page) by index.
- set_active_page: Switch active slide/page.
- set_placeholder_text: Set text on a slide placeholder by role or index (Impress).

{specialized_delegation}

{core_directives}"""


DEFAULT_DRAW_GREETING = "AI: I can help you create and edit polished, colorful shapes in Draw and Impress. Try me!"

# ---------------------------------------------------------------------------
# Assembly (dispatch + late init)
# ---------------------------------------------------------------------------

DEFAULT_CHAT_SYSTEM_PROMPT = ""
DEFAULT_CALC_CHAT_SYSTEM_PROMPT = ""
DEFAULT_DRAW_CHAT_SYSTEM_PROMPT = ""


def get_core_directives(model) -> str:
    """Return the application-specific core directives dynamically based on document type."""
    from plugin.doc.doc_type import is_calc, is_draw
    if is_calc(model):
        return CALC_CORE_DIRECTIVES
    elif is_draw(model):
        return DRAW_CORE_DIRECTIVES
    else:
        return WRITER_CORE_DIRECTIVES


def _catalog_entries_from_base(base_cls, *, agent_label: str | None = None, ctx=None) -> list[dict[str, str]]:
    """Build ``[{domain, description}, …]`` for one specialized base class (delegate/MCP catalog)."""
    entries: list[dict[str, str]] = []
    for cls in base_cls.__subclasses__():
        domain = getattr(cls, "specialized_domain", None)
        desc = getattr(cls, "specialized_domain_description", None)
        if not domain:
            continue
        if agent_label == "Calc" and domain in CALC_HIDDEN_SPECIALIZED_DOMAINS:
            continue
        if agent_label == "Writer" and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
            continue
        if agent_label == "Draw" and domain in IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS:
            continue
        if domain == "vision" and ctx is not None:
            from plugin.vision.vision_availability import vision_venv_configured

            if not vision_venv_configured(ctx):
                continue
        entries.append({"domain": str(domain), "description": str(desc or "")})
    return entries


def get_specialized_domain_catalog(*, agent_label: str | None, ctx=None) -> list[dict[str, str]]:
    """Full specialized domain catalog — same entries as sidebar/delegate domain hints.

    ``agent_label`` is ``Writer`` / ``Calc`` / ``Draw`` for one app, or ``None`` to merge
    all three (e.g. MCP ``find_tools`` with no document open).
    """
    if agent_label == "Calc":
        from plugin.calc.base import ToolCalcSpecialBase

        entries = _catalog_entries_from_base(ToolCalcSpecialBase, agent_label="Calc", ctx=ctx)
    elif agent_label == "Draw":
        from plugin.draw.base import ToolDrawSpecialBase

        entries = _catalog_entries_from_base(ToolDrawSpecialBase, agent_label="Draw", ctx=ctx)
    elif agent_label == "Writer":
        from plugin.writer.specialized_base import ToolWriterSpecialBase

        entries = _catalog_entries_from_base(ToolWriterSpecialBase, agent_label="Writer", ctx=ctx)
    else:
        from plugin.calc.base import ToolCalcSpecialBase
        from plugin.draw.base import ToolDrawSpecialBase
        from plugin.writer.specialized_base import ToolWriterSpecialBase

        seen: dict[str, str] = {}
        for base, label in (
            (ToolWriterSpecialBase, "Writer"),
            (ToolCalcSpecialBase, "Calc"),
            (ToolDrawSpecialBase, "Draw"),
        ):
            for entry in _catalog_entries_from_base(base, agent_label=label, ctx=ctx):
                dom = entry["domain"]
                desc = entry["description"]
                if dom not in seen or len(desc) > len(seen[dom]):
                    seen[dom] = desc
        return [{"domain": dom, "description": seen[dom]} for dom in sorted(seen)]
    entries.sort(key=lambda e: e["domain"])
    return entries


def _get_specialized_domains_str(base_cls, *, agent_label: str | None = None, ctx=None) -> str:
    """Build a compact domain list for delegation hints and MCP schemas."""
    parts = []
    for entry in sorted(_catalog_entries_from_base(base_cls, agent_label=agent_label, ctx=ctx),
                        key=lambda e: e["domain"]):
        if entry["description"]:
            parts.append(f"{entry['domain']}: {entry['description']}")
        else:
            parts.append(entry["domain"])
    return "; ".join(parts)


def _specialized_delegation_template_for_label(agent_label: str) -> str:
    if agent_label == "Calc":
        return CALC_SPECIALIZED_DELEGATION_TEMPLATE
    if agent_label == "Draw":
        return DRAW_SPECIALIZED_DELEGATION_TEMPLATE
    return WRITER_SPECIALIZED_DELEGATION_TEMPLATE


def get_specialized_delegation_for_model(model, ctx=None) -> str:
    """Specialized-delegation block for chat system prompt (same text as MCP delegate tool hint)."""
    from plugin.doc.doc_type import is_calc, is_draw

    if is_calc(model):
        from plugin.calc.base import ToolCalcSpecialBase

        return get_specialized_delegation_tool_hint(ToolCalcSpecialBase, "Calc", ctx=ctx)
    if is_draw(model):
        from plugin.draw.base import ToolDrawSpecialBase

        return get_specialized_delegation_tool_hint(ToolDrawSpecialBase, "Draw", ctx=ctx)
    from plugin.writer.specialized_base import ToolWriterSpecialBase

    return get_specialized_delegation_tool_hint(ToolWriterSpecialBase, "Writer", ctx=ctx)


def format_specialized_domains_description(special_base_class, *, agent_label: str | None = None, ctx=None) -> str:
    """Domain enum help for MCP/OpenAPI (more compact than the full delegation hint)."""
    domains = _get_specialized_domains_str(special_base_class, agent_label=agent_label, ctx=ctx)
    if not domains:
        return "The specialized domain to activate."
    # Compact form for the enum property description to reduce bloat in MCP schema
    compact = domains.replace("; ", ", ")
    return f"domain one of: {compact}"


def get_specialized_delegation_tool_hint(special_base_class, agent_label: str, *, ctx=None) -> str:
    """Full specialized-delegation guidance (sidebar system prompt and MCP ``tools/list``)."""
    domains_str = _get_specialized_domains_str(special_base_class, agent_label=agent_label, ctx=ctx)
    template = _specialized_delegation_template_for_label(agent_label)
    return template.format(domains=domains_str)


def get_vision_core_directive(model, ctx) -> str:
    """OCR delegation hint when local vision stack is configured (Writer/Calc only)."""
    if ctx is None:
        return ""
    from plugin.doc.doc_type import is_calc, is_writer
    from plugin.vision.vision_availability import vision_venv_configured

    if not vision_venv_configured(ctx):
        return ""
    if not (is_writer(model) or is_calc(model)):
        return ""
    delegate = "delegate_to_specialized_calc_toolset" if is_calc(model) else "delegate_to_specialized_writer_toolset"
    return (
        f"When the user wants OCR or text from an embedded image, {delegate}(domain=\"vision\", task=\"\"). "
        "That runs local OCR on the selected graphic and inserts the recognized text into the document "
        "(no sub-agent; task is ignored). You must use this call to perform OCR."
    )


def get_greeting_for_document(model):
    """Return a greeting relevant to the document type."""
    from plugin.framework.i18n import _
    from plugin.doc.doc_type import is_calc, is_draw

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
    from plugin.doc.doc_type import is_calc, is_draw

    _ensure_venv_import_policy_strings()
    delegation = get_specialized_delegation_for_model(model, ctx=ctx)

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

    vision_directive = get_vision_core_directive(model, ctx)
    if vision_directive:
        base += "\n\n" + vision_directive

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

        # Humanizer skill (minimal addition, re-uses the exact same injection pattern as memory above).
        # When enabled, the model receives the rules as ambient context for any prose it generates
        # or revises. This is the primary delivery mechanism — cheap, always-on when active,
        # and automatically benefits main chat + all writing/brainstorming sub-agents.
        # User can turn it off in Settings or override the rules by editing the SKILL.md file.
        # Defined in plugin/chatbot/module.yaml so it appears as a checkbox in the sidebar Settings.
        try:
            from plugin.chatbot.skills import SkillStore
            from plugin.framework.config import get_config_bool_safe

            if get_config_bool_safe("chatbot.humanizer_enabled"):
                hstore = SkillStore(ctx)
                hguidance = hstore.get_humanizer_guidance()
                if hguidance:
                    base += "\n\n[HUMANIZER GUIDANCE — apply when generating or revising prose]\n" + hguidance.strip() + "\n"
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Failed to inject humanizer guidance: {e}")

    if additional_instructions and str(additional_instructions).strip():
        return base + "\n\n" + str(additional_instructions).strip()
    return base


def _ensure_venv_import_policy_strings() -> None:
    """Fill venv-policy prompt strings on first use (import_policy pulls smolagents)."""
    if _VENV_IMPORT_POLICY_COMPACT:
        return
    _init_venv_import_policy_strings()


def _init_venv_import_policy_strings() -> None:
    """Late init: import_policy pulls smolagents; constants must be fully loaded first."""
    global _VENV_IMPORT_POLICY_COMPACT, _VENV_IMPORT_POLICY_FULL
    global PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE, PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE
    global CALC_FORMULA_SYNTAX, DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE, CALC_PYTHON_FORMULA_LLM_HINT

    from plugin.scripting.import_policy import format_venv_import_policy_for_prompt

    compact = format_venv_import_policy_for_prompt(compact=True)
    _VENV_IMPORT_POLICY_COMPACT = compact
    _VENV_IMPORT_POLICY_FULL = format_venv_import_policy_for_prompt(compact=False)
    PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE = compact + " "
    PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE = compact
    CALC_FORMULA_SYNTAX = f"""FORMULA SYNTAX: LibreOffice formulas use semicolon (;) between arguments, not comma.
Other-sheet refs use a dot (Orders.A1), never Excel bang (Orders!A1 → #NAME?).
- Correct: =SUM(A1:A10), =IF(A1>0;B1;C1), =PY("result = …"; Orders.A1:H500)
- Wrong: =IF(A1>0,"Yes","No"), Orders!A1
- =PY("result = …"; DataRange) writes Python into a cell (omit DataRange if unused).
{compact}
- Example: =PY("result = np.sum(data)"; Orders.A1:H500).
- For tables with headers, use data.to_pandas() instead of pd.DataFrame(data) so row 0 becomes column headers rather than synthetic numeric columns (0..N).
- {CALC_PYTHON_DATA_SHAPE_LLM_HINT}"""
    # Phase 6 spreadsheet-import LLM fallback; not a second prompt.
    CALC_PYTHON_FORMULA_LLM_HINT = CALC_FORMULA_SYNTAX
    DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE = _build_calc_chat_system_prompt_template()
