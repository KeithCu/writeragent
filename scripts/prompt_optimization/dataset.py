"""
Fixed examples for prompt optimization / eval (scripts/prompt_optimization/).

ALL_EXAMPLES is 17 tasks: 12 Writer (including style_consistency, smart_summarization,
section_refactor, comment_management) + flowchart_gen (Draw) + data_sorting / tax_column
(Calc) + two Phase F =PY dest rows (refuse overlap, no bulk read).
Structural tasks are scored from the exported final document (oracles + honest substring
checks). A quality judge runs after the hard gate for resume, rewriting, summarization,
and the two table tasks. See docs/eval/eval-dev-plan.md.
"""
import sys
from pathlib import Path

# Allow importing from repo root (for constants)
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# ---------------------------------------------------------------------------
# 1. Table from mess (cleanup and make pretty)
# ---------------------------------------------------------------------------
MESSY_TABLE_INPUT = """* Battery|Battle Born BB5024H (24V 50Ah Heated)|$999.00[3]|The heart of the system. 10-year warranty.

Controller|Victron SmartSolar MPPT 100/30|$135.15[2]|Handles the 440W panel easily at 24V.

* USB Charger|Blue Sea Systems 1045 (4.8A)|$43.00|Industrial grade. Accepts 24V input directly.

* PoE Converter|Tycon TP-DCDC-1224G-4P|$66.00|Critical: Stabilizes 24V battery voltage (which swings 20V-29V) to a clean 24V PoE for the Ubiquiti.

Enclosure|Saginaw SCE-202010ELJ|$215.31|20x20x10 NEMA 4 steel box.
"""

TABLE_FROM_MESS = {
    "document_content": MESSY_TABLE_INPUT,
    "user_question": "Convert this messy parts list into a clean HTML table with headings and a total price.",
    "task_id": "table_from_mess",
    "expected_contains": ["Battle Born", "Victron", "SmartSolar", "NEMA 4", "Total"],
    "is_non_trivial": True,
    "category": "structural",
    "use_quality_judge": True,
    "rubric": "Output must be an HTML table (not a list). It should have clear column headings, one row per unique item, a total entry, and preserve all prices exactly.",
}

# ---------------------------------------------------------------------------
# 2. Reformat resume
# ---------------------------------------------------------------------------
PLAIN_RESUME = """john doe
john@example.com | 555-1234

SUMMARY
I am a very dedicated developer who has worked at many places and I really love coding in Python and doing APIs. I led some people once and it was good. I have experience in both front-end and back-end stuff and I am looking for a new job.

WORK HISTORY
* acme corp 2020 to 2023 developer
  built apis and fixed bugs led 2 junior devs. we used python mostly.

- techstart inc Feb '23-present senior developer
  microservices architecture ci/cd on-call rotation. worked on high scale stuff with high availability requirements
  We scaled the system to 100K users and 100M requests per month using a novel caching strategy.

EDUCATION
state university bs computer science 2016 gpa 3.8

* skills
python java sql docker kubernetes
certifications: AWS, Kubernetes
"""

REFORMAT_RESUME = {
    "document_content": PLAIN_RESUME,
    "user_question": "Reformat this plain text resume as professional HTML. Keep the person's name at the top. Use EXACT section headings: WORK HISTORY, EDUCATION, SKILLS (no variations like 'Work Experience' or 'Summary'). Consistent list items for all experience, active voice in summary (<=60 words, mention Python APIs and leadership), bold job titles/roles, consistent date formatting (e.g. '2020-2023'). Keep the 100K users / 100M requests achievement. Fix all inconsistencies (casing, spacing, bullets, certifications).",
    "task_id": "reformat_resume",
    "expected_contains": [
        "John Doe",
        "WORK HISTORY",
        "EDUCATION",
        "SKILLS",
        "Acme Corp",
        "TechStart Inc",
        "100K",
        "100M",
    ],
    "is_non_trivial": True,
    "category": "creative",
    "use_quality_judge": True,
    "rubric": "Professional resume format. Creative: 50% accuracy (exact headings, all content captured/fixed without loss including 100K/100M scale), 20% formatting (valid HTML, consistent lists/dates), 30% naturalness (active voice, professional tone). Matches gold_standards.json.",
}

# ---------------------------------------------------------------------------
# 3. Table Engineering (CSV-like to table)
# ---------------------------------------------------------------------------
CSV_LIKE = """Fruit, Price, Qty
Apple, 1.20, 12
Banana, 0.50, 24
Orange, 0.80
Grape, 2.00, 8
Mango, 1.50, 6, [note]
Kiwi,1.75,,
Total,?,?
"""

TABLE_ENGINEERING = {
    "document_content": CSV_LIKE,
    "user_question": "Convert this comma-separated (with irregularities, footnotes, missing values) list into a clean HTML table with headers (Item, Price, Quantity). Missing Quantity is 0 (do not invent a count for Orange or Kiwi). Ignore the [note] cell as a quantity (do not put [note] in Quantity). Add a Total row: sum of Price×Quantity. Right-align numerics.",
    "task_id": "table_engineering",
    "expected_contains": ["Item", "Price", "Quantity", "Total", "Kiwi"],
    "is_non_trivial": True,
    "category": "structural",
    "use_quality_judge": True,
    "rubric": "Clean CSV-to-table conversion with edge cases. Structural: 60% accuracy (correct totals, handle notes/missing data without hallucination), 40% formatting (HTML table, right-aligned nums, Total row). Map 'Fruit'->'Item'.",
}

# ---------------------------------------------------------------------------
# 4. Bulk Cleanup
# ---------------------------------------------------------------------------
DOUBLE_SPACE_TEXT = """This  sentence   has    extra   spaces.  So  does  this  one..
Another   paragraph   here  ,  with spaces before commas.  Fix  all  double  spaces  and  ensure  one  space  after  sentences.
https://example.com/test  with   URL. "Quoted  text"  should  stay  intact .

Too many line breaks above  .  Normalize to single paragraph breaks. Also fix this one with trailing  period  .

CMD: git  log  --oneline
"""

BULK_CLEANUP = {
    "document_content": DOUBLE_SPACE_TEXT,
    "user_question": "Remove all double spaces and fix punctuation (no space before comma, no double periods) except on the CMD: line, which must stay exactly as written. Normalize line breaks to single paragraph breaks. Keep the URL.",
    "task_id": "bulk_cleanup",
    # Visible-text oracles catch leftover double spaces. Do not reject a lone " "
    # (every English sentence has one) or raw HTML indent from LO export.
    "expected_contains": [
        "This sentence has extra spaces",
        "https://example.com/test",
        "Quoted text",
    ],
    "reject_contains": [" .", "..", " ,", "period  ."],
    "category": "structural",
    "rubric": "Perfect normalization. Structural: 60% accuracy (no meaning loss, preserve URLs/quotes exactly), 40% formatting (clean HTML paragraphs, zero forbidden patterns). Use apply_document_content(target='full_document'). Matches gold.",
}

# ---------------------------------------------------------------------------
# 5. Logical Rewriting
# ---------------------------------------------------------------------------
TECH_PARAGRAPH = """We are incredibly excited to announce the release of WriterAgent version 2.0, a significant leap forward in our mission to provide the most powerful local AI editing experience for word processors. This update introduces a brand new, sophisticated 'Judge' system that leverages multi-dimensional scoring models to provide more accurate and consistent evaluations of model performance. By utilizing frameworks like G-Eval and Prometheus, we've moved beyond simple string matching to a nuanced analysis of semantic correctness, formatting fidelity, and naturalness. Furthermore, version 2.0 includes a new 'Dual-Mode' evaluation system that intelligently distinguishes between structural tasks like table generation and creative tasks like logical rewriting, applying weighted criteria specifically tailored to each task type. We've also optimized our OpenRouter integration to support the latest model releases, including the Qwen 3.5 and Gemini 3 Flash series. Download the update today to experience the future of local AI-assisted writing."""

LOGICAL_REWRITING = {
    "document_content": TECH_PARAGRAPH,
    "user_question": "Rewrite this paragraph to be professional and concise (≤70 words). Preserve 'WriterAgent', '2.0', 'Dual-Mode', 'G-Eval' and 'Prometheus' verbatim. Exclude all hype words like 'incredibly', 'significant leap', 'brand new'. Use active voice.",
    "task_id": "logical_rewriting",
    "expected_contains": ["WriterAgent", "2.0", "Dual-Mode", "G-Eval", "Prometheus"],
    "reject_contains": ["LocalWriter", "incredibly", "significant leap", "brand new"],
    "is_non_trivial": True,
    "category": "creative",
    "use_quality_judge": True,
    "rubric": "Professional, concise rewrite. Creative: 50% accuracy (exact terms preserved, no hype, ≤70 words), 20% formatting, 30% naturalness (active voice, flows well).",
}

# ---------------------------------------------------------------------------
# 6. Format Preservation (replace text)
# ---------------------------------------------------------------------------
HEADER_TEXT = """John Doe - Project Lead

Contact person: John Doe (legacy ID JD-001). Do not change this legal name on this line."""

FORMAT_PRESERVATION = {
    "document_content": HEADER_TEXT,
    "user_question": (
        "Replace 'John Doe' with 'Jane Smith' only in the first line (the role title). "
        "Leave the second line exactly as written, including the name on that line."
    ),
    "task_id": "format_preservation",
    "expected_contains": [
        "Jane Smith - Project Lead",
        "Contact person: John Doe (legacy ID JD-001)",
    ],
    "reject_contains": [
        "John Doe - Project Lead",
        "Jane Smith (legacy ID JD-001)",
    ],
    "category": "structural",
}

# ---------------------------------------------------------------------------
# 7. Style Application (heading)
# ---------------------------------------------------------------------------
INTRO_TEXT = """Project Overview (draft)

Introduction

This section explains the scope. Do not promote Background or Summary to the same heading level.

Background

Earlier work used a monolith.

Summary

We will refactor in phases."""

STYLE_APPLICATION = {
    "document_content": INTRO_TEXT,
    "user_question": (
        "Make only Introduction a Heading 1. Leave Background and Summary as body text."
    ),
    "task_id": "style_application",
    "expected_contains": ["Introduction", "Background", "Summary"],
    "reject_contains": [],
    "category": "structural",
}

# ---------------------------------------------------------------------------
# 8. Bullet consistency
# ---------------------------------------------------------------------------
BULLET_LIST = """* Pack the crate
- Ship to Oslo  
3) Call the depot
• Label the pallet
- Sweep  the  bay
* File the docket.
- Seal the hatch
Note: do not bullet this line
"""

BULLET_CONSISTENCY = {
    "document_content": BULLET_LIST,
    "user_question": (
        "Make this a single consistent bullet list, one period per item. "
        "Leave the Note as a normal paragraph, not a bullet."
    ),
    "task_id": "bullet_consistency",
    "expected_contains": [
        "Pack the crate",
        "Ship to Oslo",
        "Call the depot",
        "Label the pallet",
        "Sweep the bay",
        "File the docket",
        "Seal the hatch",
        "Note:",
    ],
    "reject_contains": ["3) Call", ".."],
    "category": "structural",
    "rubric": "Perfect list normalization. Structural: 60% accuracy (all 7 items preserved exactly, no variants left), 40% formatting (consistent - bullets + period, clean HTML <ul> preferred). Zero rejects. Uses targeted apply_document_content.",
}

# ---------------------------------------------------------------------------
# Additional tests from docs/archive/eval/ideas.md (string-backend compatible; some hardened)
# ---------------------------------------------------------------------------

# Style Consistency (archive Writer #12, #18)
STYLE_CONSISTENCY = {
    "document_content": (
        '<p data-lo-style="Default">Default style paragraph one.</p>'
        '<h2 data-lo-style="Heading2">HEADING 2 text that should be upgraded.</h2>'
        '<p data-lo-style="Default">Another default paragraph.</p>'
        "<h2>Heading 2 again.</h2>"
    ),
    "user_question": (
        "Change every Default paragraph to the Quotations style. "
        "Promote both Heading 2 lines to Heading 1. Do not make the Default paragraphs Heading 1. "
        "Keep the original sentence text."
    ),
    "task_id": "style_consistency",
    "expected_contains": ["Quotations", "HEADING 2 text", "Heading 2 again"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Default -> Quotations as a style token, not a stuffed word. Heading 2 -> H1. Preserve content.",
}

# Smart Summarization (archive Writer #15)
SMART_SUMMARIZATION = {
    "document_content": """# Findings
The system achieved 99.9% uptime. Latency averaged 45ms under load. Error rate was 0.01%. Scaling tests confirmed linear performance to 10k RPS. Cost per query dropped 40% after optimization.
A canary deploy failed with a 12% error spike and was rolled back. The intern joked that p95 latency was 9001ms.

# Executive Summary
[To be filled by agent]
""",
    "user_question": (
        "Summarize Findings into exactly 5 bullet points in Executive Summary covering "
        "uptime, latency, error rate, scale, and cost. Do not include the canary failure "
        "or the intern joke."
    ),
    "task_id": "smart_summarization",
    "expected_contains": ["Executive Summary", "99.9%", "45ms", "0.01%", "10k", "40%"],
    "is_non_trivial": True,
    "category": "creative",
    "use_quality_judge": True,
    "rubric": "Five real stats in Executive Summary as bullets; distractors stay out of that section.",
}

# Section Refactor (archive Writer #17)
SECTION_REFACTOR = {
    "document_content": """# Introduction
Background info here.

# Body
Main content goes here. See the Conclusion for next steps.

# Conclusion
Final thoughts and call to action.
""",
    "user_question": "Move the 'Conclusion' after the 'Introduction' and rename it 'Goal'. Update any cross-references if present.",
    "task_id": "section_refactor",
    # Headings must survive HTML apply / LO unwrap (not markdown-only "# Introduction").
    "expected_contains": ["Introduction", "Goal", "Body", "See the Goal"],
    "reject_contains": ["Conclusion"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Structural movement of sections with rename. Conclusion becomes Goal and placed after Intro. Update the Body cross-reference. No orphaned headings.",
}

# Comment Management Simulation (archive Writer #3; text-based for string backend)
COMMENT_MANAGEMENT = {
    "document_content": """The results are uncertain at this point in the analysis.
Further testing is recommended before deployment.""",
    "user_question": "Add a comment 'Review this before finalizing' anchored on the word 'uncertain'.",
    "task_id": "comment_management",
    "expected_contains": ["uncertain", "Review this before finalizing"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Simulate comment addition via text annotation or note (use find_text + apply). Document reflects the review note. (Full UNO comments require LO backend.)",
}

# ---------------------------------------------------------------------------
# All examples (for train/val split). TABLE_FROM_MESS kept as baseline; others hardened with stricter rubrics, edge cases, tool hints, expanded reject_contains/expected_contains for better good-vs-great differentiation.
# ---------------------------------------------------------------------------
ALL_EXAMPLES = [
    TABLE_FROM_MESS,
    REFORMAT_RESUME,
    TABLE_ENGINEERING,
    BULK_CLEANUP,
    LOGICAL_REWRITING,
    FORMAT_PRESERVATION,
    STYLE_APPLICATION,
    BULLET_CONSISTENCY,
    STYLE_CONSISTENCY,
    SMART_SUMMARIZATION,
    SECTION_REFACTOR,
    COMMENT_MANAGEMENT,
]

# Flowchart Gen (from archive/eval/ideas.md Draw #3) - tests non-LO shapes via DrawDocState
FLOWCHART_GEN = {
    "document_content": "Create a simple login flowchart.",
    "user_question": (
        "Create a login flowchart: Start oval, then a Process box for user login, then a "
        "Decision diamond 'credentials valid?', Yes to End, No back to Process. "
        "Verify the layout with get_draw_tree (nodes and connections)."
    ),
    "task_id": "flowchart_gen",
    "expected_contains": ["Start", "End", "login", "credentials"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Uses shape_upsert for flowchart-* types (oval, rectangle, diamond). Connections via shape_connect or tree. Final get_draw_tree shows proper hierarchy and connected_start/connected_end. Matches production Draw tree structure.",
}

# Data Sorting (eval/ideas.md Calc #6) - non-LO test using CalcStringState.sort_range
DATA_SORTING = {
    "document_content": (
        "Product\tRevenue\n"
        "Widget\t1200\n"
        "Gadget\t850\n"
        "Tool\t2100\n"
        "Device\t1200\n"
        "Aardvark\tn/a"
    ),
    "user_question": (
        "Sort this sheet by Revenue descending. Ties (same Revenue) go by Product "
        "ascending. Leave non-numeric Revenue rows last."
    ),
    "task_id": "data_sorting",
    "expected_contains": ["Tool", "2100", "Widget", "1200", "Aardvark"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Correct descending sort by Revenue. Final snapshot JSON shows Tool first. Uses CalcStringState.",
}

# Basic Tax Column (eval/ideas.md Calc #1, hardened) - non-LO test using CalcStringState.write_cell_range
TAX_COLUMN = {
    "document_content": (
        "Item\tPrice\tTax\n"
        "Apple\t10\t0.99\n"
        "Banana\t5\t0.99\n"
        "Orange\t8\t0.99\n"
        "Pear\t12.5\t0.99\n"
        "Note\tn/a\t\n"
        "Total\t?\t"
    ),
    "user_question": (
        "Put an 8% tax formula in the Tax column for each fruit, relative to Price. "
        "The numbers already in Tax are wrong — replace them. Leave Note and Total blank."
    ),
    "task_id": "tax_column",
    "expected_contains": ["Tax"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Writes correct tax values (Price*0.08, e.g. 0.8/0.4/0.64/1.0). Structural: 60% accuracy (precise calcs, verification step), 40% formatting (correct JSON snapshot with Tax column). Uses CalcStringState fully (no hallucinations on Total/?).",
}

# Phase F =PY dest (string-first). Fixture is small; the question keeps A1:H500 wording.
_PY_SHEET = (
    "Name\tCity\tAmt\n"
    "Ann\tX\t1\n"
    "Ann\tX\t1\n"
    "Bob\tY\t2\n"
    "Cara\tZ\t3\n"
    "Cara\tZ\t3\n"
    "Dan\tX\t4\n"
    "Eve\tY\t5"
)

_PY_RUBRIC = (
    "write_formula_range of =PY(...) into an empty cell outside DataRange A1:H500 "
    "(J1 or first empty column). Do not write onto the data range. Do not domain=python. "
    "Do not read_cell_range the whole block."
)

PY_REFUSE_OVERLAP = {
    "document_content": _PY_SHEET,
    "user_question": (
        "Put a =PY unique-rows formula in H1. Data is A1:H500. "
        "If H1 is inside the data range, write beside it instead and say so."
    ),
    "task_id": "py_refuse_overlap",
    "expected_contains": ["=PY"],
    "reject_contains": [],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": _PY_RUBRIC + " H1 is inside A1:H500 — dest must be J1/I1.",
}

PY_NO_BULK_READ = {
    "document_content": _PY_SHEET,
    "user_question": (
        "Drop duplicate rows on A1:H500 using =PY. "
        "Do not read_cell_range of A1:H500 or dump the spill into chat."
    ),
    "task_id": "py_no_bulk_read",
    "expected_contains": ["=PY"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": _PY_RUBRIC,
}

ALL_EXAMPLES.append(FLOWCHART_GEN)
ALL_EXAMPLES.append(DATA_SORTING)
ALL_EXAMPLES.append(TAX_COLUMN)
ALL_EXAMPLES.append(PY_REFUSE_OVERLAP)
ALL_EXAMPLES.append(PY_NO_BULK_READ)



def task_kind(task_id: str) -> str:
    """Factory kind for an eval task_id (writer / draw / calc).

    Kind is keyed by task_id, not question keywords — flowchart_gen is Draw,
    data_sorting and tax_column are Calc, everything else is Writer.
    """
    if task_id == "flowchart_gen":
        return "draw"
    if task_id in (
        "data_sorting",
        "tax_column",
        "py_refuse_overlap",
        "py_no_bulk_read",
    ):
        return "calc"
    return "writer"


def to_eval_examples(examples=None):
    """Attribute-access examples without requiring dspy (scripted / LlmClient path)."""
    from types import SimpleNamespace

    if examples is None:
        examples = ALL_EXAMPLES
    out = []
    for ex in examples:
        out.append(
            SimpleNamespace(
                document_content=ex["document_content"],
                user_question=ex["user_question"],
                task_id=ex.get("task_id", ""),
                expected_contains=ex.get("expected_contains", []),
                reject_contains=ex.get("reject_contains", []),
                rubric=ex.get("rubric", ""),
                gold_document=ex.get("gold_document", ""),
                is_non_trivial=ex.get("is_non_trivial", False),
                category=ex.get("category", "structural"),
                use_quality_judge=ex.get("use_quality_judge", False),
            )
        )
    return out


def _load_gold_standards(examples: list[dict]) -> list[dict]:
    """Load gold documents from gold_standards.json if it exists."""
    import json
    p = Path(__file__).parent / "gold_standards.json"
    if not p.exists():
        return examples
    try:
        golds = json.loads(p.read_text(encoding="utf-8"))
        for ex in examples:
            tid = ex.get("task_id")
            if tid in golds:
                ex["gold_document"] = golds[tid]
    except Exception as e:
        print(f"Warning: Failed to load gold_standards.json: {e}")
    return examples


ALL_EXAMPLES = _load_gold_standards(ALL_EXAMPLES)


def to_dspy_examples(examples=None, with_inputs=True):
    """Convert dict examples to dspy.Example objects. Requires dspy."""
    import dspy
    if examples is None:
        examples = ALL_EXAMPLES
    out = []
    for ex in examples:
        e = dspy.Example(
            document_content=ex["document_content"],
            user_question=ex["user_question"],
            task_id=ex.get("task_id", ""),
            expected_contains=ex.get("expected_contains", []),
            reject_contains=ex.get("reject_contains", []),
            rubric=ex.get("rubric", ""),
            gold_document=ex.get("gold_document", ""),
            is_non_trivial=ex.get("is_non_trivial", False),
            category=ex.get("category", "structural"),
            use_quality_judge=ex.get("use_quality_judge", False),
        ).with_inputs("document_content", "user_question", "task_id") if with_inputs else dspy.Example(**ex)
        out.append(e)
    return out


def parse_task_id_filter(spec: str | None) -> list[str] | None:
    """Comma-separated ``-e`` list, or None when unset."""
    if spec is None or not str(spec).strip():
        return None
    return [part.strip() for part in str(spec).split(",") if part.strip()]


def filter_examples(examples=None, task_ids=None, n=None):
    """Filter ALL_EXAMPLES (or ``examples``) by task_id list and optional cap."""
    if examples is None:
        examples = ALL_EXAMPLES
    out = list(examples)
    if task_ids:
        wanted = set(task_ids)
        out = [ex for ex in out if ex.get("task_id", "") in wanted]
    if n is not None:
        out = out[: int(n)]
    return out


def get_trainset_valset(split=0.8, seed=42, examples=None):
    """Split examples into train and val. Returns (trainset, valset) as list of dicts."""
    import random
    pool = list(ALL_EXAMPLES if examples is None else examples)
    rng = random.Random(seed)
    indices = list(range(len(pool)))
    rng.shuffle(indices)
    n = int(len(pool) * split)
    train_idx = set(indices[:n])
    trainset = [pool[i] for i in range(len(pool)) if i in train_idx]
    valset = [pool[i] for i in range(len(pool)) if i not in train_idx]
    return trainset, valset
