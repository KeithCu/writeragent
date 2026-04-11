"""
Fixed examples for DSPy prompt optimization.
Each example has document_content (the in-memory "document") and user_question.
Optional: expected_contains (list of strings that must appear in final doc) for metric.
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

Tycon TP-DCDC-1224G-4P|$66.00|Critical: Stabilizes 24V battery voltage (which swings 20V-29V) to a clean 24V PoE for the Ubiquiti.

Enclosure|Saginaw SCE-202010ELJ|$215.31|20x20x10 NEMA 4 steel box.
"""

TABLE_FROM_MESS = {
    "document_content": MESSY_TABLE_INPUT,
    "user_question": "Convert this messy parts list into a clean HTML table with headings and a total price.",
    "task_id": "table_from_mess",
    "expected_contains": ["Battle Born", "Victron", "SmartSolar", "NEMA 4"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Output must be an HTML table (not a list). It should have clear column headings, one row per unique item, a total entry, and preserve all prices exactly.",
}

# ---------------------------------------------------------------------------
# 2. Reformat resume
# ---------------------------------------------------------------------------
PLAIN_RESUME = """john doe
john@example.com  |  555-1234

SUMMARY
I am a very dedicated developer who has worked at many places and I really love coding in Python and doing APIs. I led some people once and it was good. I have experience in both front-end and back-end stuff and I am looking for a new job.

WORK HISTORY
* acme corp 2020 to 2023  developer
  built apis and fixed bugs  led 2 junior devs. we used python mostly.

- techstart inc  Feb '23-present  senior developer
  microservices architecture  ci/cd  on-call rotation. worked on high scale stuff with high availability requirements
  We scaled the system to 100K users and 100M requests per month using a novel caching strategy.

EDUCATION
state university  bs computer science 2016  gpa 3.8

* skills
python  java  sql  docker  kubernetes
"""

REFORMAT_RESUME = {
    "document_content": PLAIN_RESUME,
    "user_question": "Reformat this plain text resume as professional. Use clear section headings and consistent formatting.",
    "task_id": "reformat_resume",
    "expected_contains": ["John", "Work", "Skills", "Education", "Acme", "TechStart"],
    "is_non_trivial": True,
    "category": "creative",
    "rubric": "Professional resume format. Clear section headings (WORK HISTORY, EDUCATION, SKILLS). Consistent bullet points for all work experience items.",
}

# ---------------------------------------------------------------------------
# 3. Table Engineering (CSV-like to table)
# ---------------------------------------------------------------------------
CSV_LIKE = """Fruit, Price, Qty
Apple, 1.20, 12
Banana, 0.50, 24
Orange, 0.80
Grape, 2.00, 8
Mango, 1.50, 6,
"""

TABLE_ENGINEERING = {
    "document_content": CSV_LIKE,
    "user_question": "Convert this comma-separated list into a clean table with headers (Item, Price, Quantity). Fix missing or extra commas.",
    "task_id": "table_engineering",
    "expected_contains": ["Item", "Price", "Quantity"],
    "is_non_trivial": True,
    "category": "structural",
    "rubric": "Clean CSV-to-table conversion. Map 'Fruit' to 'Item'. Ensure numeric values are right-aligned if possible, or at least consistent.",
}

# ---------------------------------------------------------------------------
# 4. Bulk Cleanup
# ---------------------------------------------------------------------------
DOUBLE_SPACE_TEXT = """This  sentence   has    extra   spaces.  So  does  this  one..
Another   paragraph   here  ,  with spaces before commas.  Fix  all  double  spaces  and  ensure  one  space  after  sentences.


Too many line breaks above  .  Normalize to single paragraph breaks.
"""

BULK_CLEANUP = {
    "document_content": DOUBLE_SPACE_TEXT,
    "user_question": "Remove all double spaces, fix punctuation (no space before comma, no double periods), and normalize line breaks to single paragraph breaks.",
    "task_id": "bulk_cleanup",
    "expected_contains": [],
    "reject_contains": ["  ", " .", "..", " ,"],  # no double spaces, space-before-period, double period, space before comma
    "category": "structural",
}

# ---------------------------------------------------------------------------
# 5. Logical Rewriting
# ---------------------------------------------------------------------------
TECH_PARAGRAPH = """We are incredibly excited to announce the release of LocalWriter version 2.0, a significant leap forward in our mission to provide the most powerful local AI editing experience for word processors. This update introduces a brand new, sophisticated 'Judge' system that leverages multi-dimensional scoring models to provide more accurate and consistent evaluations of model performance. By utilizing frameworks like G-Eval and Prometheus, we've moved beyond simple string matching to a nuanced analysis of semantic correctness, formatting fidelity, and naturalness. Furthermore, version 2.0 includes a new 'Dual-Mode' evaluation system that intelligently distinguishes between structural tasks like table generation and creative tasks like logical rewriting, applying weighted criteria specifically tailored to each task type. We've also optimized our OpenRouter integration to support the latest model releases, including the Qwen 3.5 and Gemini 3 Flash series. Download the update today to experience the future of local AI-assisted writing."""

LOGICAL_REWRITING = {
    "document_content": TECH_PARAGRAPH,
    "user_question": "Rewrite this paragraph to be professional and concise.",
    "task_id": "logical_rewriting",
    "expected_contains": ["LocalWriter", "2.0"],
    "is_non_trivial": True,
    "category": "creative",
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
        "Apply Heading 1 only to the standalone section title 'Introduction' (the line between "
        "the parenthetical header and the explanatory paragraph). Leave Background and Summary "
        "as normal body text, not H1."
    ),
    "task_id": "style_application",
    "expected_contains": ["<h1>Introduction</h1>", "Background", "Summary"],
    "reject_contains": ["<h1>Background", "<h1>Summary"],
    "category": "structural",
}

# ---------------------------------------------------------------------------
# 8. Bullet consistency
# ---------------------------------------------------------------------------
BULLET_LIST = """* First thing
- Second thing  
3) Third thing
• Fourth thing
"""

BULLET_CONSISTENCY = {
    "document_content": BULLET_LIST,
    "user_question": (
        "Normalize this list: use hyphen bullets (-), one item per line, trim stray spaces, "
        "and end each bullet line with a period."
    ),
    "task_id": "bullet_consistency",
    "expected_contains": [
        "- First thing.",
        "- Second thing.",
        "- Third thing.",
        "- Fourth thing.",
    ],
    "reject_contains": ["* First", "3) Third", "• Fourth"],
    "category": "structural",
}

# ---------------------------------------------------------------------------
# All examples (for train/val split)
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
]


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
        ).with_inputs("document_content", "user_question") if with_inputs else dspy.Example(**ex)
        out.append(e)
    return out


def get_trainset_valset(split=0.8, seed=42):
    """Split ALL_EXAMPLES into train and val. Returns (trainset, valset) as list of dicts."""
    import random
    rng = random.Random(seed)
    indices = list(range(len(ALL_EXAMPLES)))
    rng.shuffle(indices)
    n = int(len(ALL_EXAMPLES) * split)
    train_idx = set(indices[:n])
    trainset = [ALL_EXAMPLES[i] for i in range(len(ALL_EXAMPLES)) if i in train_idx]
    valset = [ALL_EXAMPLES[i] for i in range(len(ALL_EXAMPLES)) if i not in train_idx]
    return trainset, valset
