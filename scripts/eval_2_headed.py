#!/usr/bin/env python3
# WriterAgent - headed eval-2 / AFC tool-round budget
"""Temporarily write chatbot.max_tool_rounds, then restore.

No new yaml knobs. Everyday chat stays at the schema default (15).
Schema max is 200 so a trial can temporarily set 80 or 200 without clamp.
AFC / Tenant / Cadaver / Long Writer pack / Draw-primary /
python-shapes-flag still write **50**. GMP Change Control, Writer→Calc
Floorstand, Reverse Tenant (Theatre CBA), and Calc-primary model write
**150**.

``--launch`` (default ``--task afc``) copies only the Population ODS into a
clean trial directory (default ``$TMP/writeragent-eval2-afc``) so
``document_research`` cannot see prompt/rubric/gold or fixture siblings.

``--task tenant-retention --launch`` copies the renewal letter ODT and exit
survey XLSX into ``$TMP/writeragent-eval2-tenant``, writes a blank
``Tenant Retention Strategy.odt``, and opens Writer.

``--task cadaver-proposal --launch`` copies ``Cadaver Budget.xlsx`` into
``$TMP/writeragent-eval2-cadaver``, writes a blank
``Collaborative Cadaver Program Proposal.odt``, and opens Writer. The
budget is research-only — do not treat it as a second write.

``--task gmp-change-control --launch`` copies the COA PDF + Material Spec
ODT + editable Draw form stand-in into ``$TMP/writeragent-eval2-gmp``,
writes a blank ``MR Risk Assessment Summary.odt``, and opens **both**
the Writer memo and the Draw form (v1 pre-open cheat). The gold PDF is
**not** the write target. COA / spec are research-only.

``--task writer-calc-peer-write --launch`` copies the email-trail ODT +
original store-list ODS + final-matrix ODS + empty budget scaffold into
``$TMP/writeragent-eval2-writer-calc``, writes a blank
``Draft Floorstand Email.odt``, and opens **both** the Writer draft and
the Calc workbook (v1 pre-open cheat). The gold deliverable xlsx is
**not** the write target. Store lists / email trail are research-only.

``--task calc-primary-model --launch`` copies only the Raw Data ODS into
``$TMP/writeragent-eval2-calc-primary`` and opens that copy in Calc
(five schedule sheets + formula fill).

``--task reverse-tenant --launch`` copies the CBA excerpt ODT and sample
roster XLSX into ``$TMP/writeragent-eval2-reverse-tenant``, writes a blank
``Theatre CBA.ods``, and opens the Writer brief then the Calc workbook
(Calc last / prompt in the Calc sidebar). The Writer excerpt is research-only.

``--task long-writer-pack --launch`` copies the two native research ODTs
into ``$TMP/writeragent-eval2-long-writer``, writes a blank
``Northhaven Civic Library Capital Brief.odt``, and opens Writer. No
peer.

``--task draw-primary --launch`` copies only ``Process Flow Map.odg``
into ``$TMP/writeragent-eval2-draw`` and opens that canvas. The gold
PDF is **not** the write target. Writer is optional/absent.

``--task python-shapes-flag --launch`` writes a blank ``American Flag.odt``
into ``$TMP/writeragent-eval2-python-shapes-flag`` and opens that Writer
document. Shapes go on its draw page. No research fixture. The string
harness cannot run this Ask (no ``run_venv`` / ``domain=python``).
On Enter / Ctrl-C the stamp also receives a copy of that ``.odt`` as
``final_flag.odt``. Save in Writer first so the file includes
LibreOffice's ``Thumbnails/thumbnail.png``. ``--score`` copies that
thumbnail to ``preview.png`` in the stamp.

Do not open ``fixtures/`` or the task folder.

``--launch`` copies the live LO-user ``writeragent_debug.log`` when the
trial ends (Enter / Ctrl-C). Pass ``--run-dir`` to write it under that
stamp; otherwise a ``YYYYMMDD-HHMM`` folder is created under the task
``runs/`` dir. Mid-stall, before restarting LO, use
``scripts/save_eval2_debug_log.py DEST_DIR``.

Usage:
  .venv/bin/python scripts/eval_2_headed.py
  .venv/bin/python scripts/eval_2_headed.py --launch
  .venv/bin/python scripts/eval_2_headed.py --launch --run-dir docs/eval/eval-2/afc-sample-83d10b06/runs/<stamp>
  .venv/bin/python scripts/eval_2_headed.py --launch --trial-dir /tmp/my-afc
  .venv/bin/python scripts/eval_2_headed.py --task tenant-retention --launch
  .venv/bin/python scripts/eval_2_headed.py --task cadaver-proposal --launch
  .venv/bin/python scripts/eval_2_headed.py --task gmp-change-control --launch
  .venv/bin/python scripts/eval_2_headed.py --task writer-calc-peer-write --launch
  .venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --launch
  .venv/bin/python scripts/eval_2_headed.py --task reverse-tenant --launch
  .venv/bin/python scripts/eval_2_headed.py --task long-writer-pack --launch
  .venv/bin/python scripts/eval_2_headed.py --task draw-primary --launch
  .venv/bin/python scripts/eval_2_headed.py --task python-shapes-flag --launch
  .venv/bin/python scripts/eval_2_headed.py -- soffice --calc workbook.ods
  .venv/bin/python scripts/eval_2_headed.py --score path/to/final_workbook.ods
  .venv/bin/python scripts/eval_2_headed.py --task tenant-retention --score path/to/final_memo.odt
  .venv/bin/python scripts/eval_2_headed.py --task cadaver-proposal --score path/to/final_proposal.odt
  .venv/bin/python scripts/eval_2_headed.py --task gmp-change-control --score path/to/final_memo.odt
  .venv/bin/python scripts/eval_2_headed.py --task writer-calc-peer-write --score path/to/final_memo.odt
  .venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --score path/to/final_workbook.ods
  .venv/bin/python scripts/eval_2_headed.py --task reverse-tenant --score path/to/final_workbook.ods
  .venv/bin/python scripts/eval_2_headed.py --task long-writer-pack --score path/to/final_pack.odt
  .venv/bin/python scripts/eval_2_headed.py --task draw-primary --score path/to/final_drawing.odg
  .venv/bin/python scripts/eval_2_headed.py --task python-shapes-flag --score path/to/runs/<stamp>/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from eval_2_debug_log import lo_user_profile_dirs, snapshot_debug_log

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_TOOL_ROUNDS_KEY = "chatbot.max_tool_rounds"
DEFAULT_MAX_TOOL_ROUNDS = 15
EVAL_2_MAX_TOOL_ROUNDS = 50
# Multidoc + peer (GMP Draw fill, Floorstand Calc write) needs more than
# the Writer-only 50-round start. Reverse Tenant and Calc-primary use
# the same 150-round start (complex payroll / five schedule sheets).
EVAL_2_GMP_MAX_TOOL_ROUNDS = 150
EVAL_2_CALC_PRIMARY_MAX_TOOL_ROUNDS = 150
_AFC_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "afc-sample-83d10b06"
_TENANT_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "tenant-retention-ed2bc14c"
_CADAVER_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "cadaver-proposal-61b0946a"
_GMP_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "gmp-change-control-58ac1cc5"
_FLOORSTAND_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "writer-calc-peer-write"
_CALC_PRIMARY_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "calc-primary-model"
_REVERSE_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "reverse-tenant"
_LONG_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "long-writer-pack"
_DRAW_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "draw-primary-deliverable"
_PYTHON_SHAPES_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "python-shapes-flag"
POPULATION_ODS_NAME = "Population v2.ods"
LETTER_ODT_NAME = "Current Renewal Letter.odt"
SURVEY_XLSX_NAME = "Exit Survey Feedback.xlsx"
TENANT_MEMO_NAME = "Tenant Retention Strategy.odt"
CADAVER_BUDGET_XLSX_NAME = "Cadaver Budget.xlsx"
CADAVER_PROPOSAL_NAME = "Collaborative Cadaver Program Proposal.odt"
GMP_COA_PDF_NAME = "Anti foam COA_MR.pdf"
GMP_SPEC_ODT_NAME = "Material Spec_MR.odt"
GMP_FORM_ODG_NAME = "Change Control Form.odg"
GMP_MEMO_NAME = "MR Risk Assessment Summary.odt"
FLOORSTAND_EMAIL_TRAIL_ODT_NAME = "Email Trail Floorstands.odt"
FLOORSTAND_ORIG_ODS_NAME = "Holiday Floorstand Store List Original.ods"
FLOORSTAND_MATRIX_ODS_NAME = "Holiday Matrix final count.ods"
FLOORSTAND_BUDGET_ODS_NAME = "Holiday Floorstand Budget.ods"
FLOORSTAND_EMAIL_NAME = "Draft Floorstand Email.odt"
FLOORSTAND_COST_SHEET = "Cost Comparison"
FLOORSTAND_STORE_SHEET = "Final Store List"
RAW_DATA_ODS_NAME = "Raw Data for Branch Profitability Final.ods"
CBA_EXCERPT_ODT_NAME = "CBA excerpt.odt"
ROSTER_XLSX_NAME = "Sample roster and schedule.xlsx"
THEATRE_CBA_ODS_NAME = "Theatre CBA.ods"
LONG_FACTS_ODT_NAME = "Northhaven Library Program Facts.odt"
LONG_DECISIONS_ODT_NAME = "Northhaven Decision Log.odt"
LONG_PACK_NAME = "Northhaven Civic Library Capital Brief.odt"
DRAW_PRIMARY_ODG_NAME = "Process Flow Map.odg"
PYTHON_SHAPES_ODT_NAME = "American Flag.odt"
# Stamp copy. --score copies Thumbnails/thumbnail.png out as preview.png.
PYTHON_SHAPES_STAMP_ODT = "final_flag.odt"
# Form-920 Section 1 blanks. Labels sit to the left so get_draw_tree
# can attach label_hint. Names stay stable for fill_draw_fields / oracle.
GMP_FILLABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("fld_change_title", "Change Title"),
    ("fld_champion", "Change Control Champion"),
    ("fld_department", "Department"),
    ("fld_date_initiated", "Date Initiated"),
    ("fld_product", "Product Description and Item Code"),
    ("fld_site", "Site Affected by Change"),
    ("fld_departments_affected", "Department/s Affected by Change"),
    ("fld_current_situation", "Current Situation"),
    ("fld_proposed_situation", "Proposed Situation"),
    ("fld_justification", "Change Justification"),
    ("fld_risk_outcome", "Outcome of Change Control Risk Assessment"),
    ("fld_risk_comments", "Risk Assessment Comments"),
)
_FIXTURE_ODS = _AFC_DIR / "fixtures" / POPULATION_ODS_NAME
_FIXTURE_CANDIDATES = (
    _FIXTURE_ODS,
    _AFC_DIR / "fixtures" / "Population v2.xlsx",
)
_TENANT_LETTER_ODT = _TENANT_DIR / "fixtures" / LETTER_ODT_NAME
_TENANT_SURVEY_XLSX = _TENANT_DIR / "fixtures" / SURVEY_XLSX_NAME
_CADAVER_BUDGET_XLSX = _CADAVER_DIR / "fixtures" / CADAVER_BUDGET_XLSX_NAME
_GMP_COA_PDF = _GMP_DIR / "fixtures" / GMP_COA_PDF_NAME
_GMP_SPEC_ODT = _GMP_DIR / "fixtures" / GMP_SPEC_ODT_NAME
_GMP_FORM_ODG = _GMP_DIR / "fixtures" / GMP_FORM_ODG_NAME
_FLOORSTAND_EMAIL_TRAIL_ODT = _FLOORSTAND_DIR / "fixtures" / FLOORSTAND_EMAIL_TRAIL_ODT_NAME
_FLOORSTAND_ORIG_ODS = _FLOORSTAND_DIR / "fixtures" / FLOORSTAND_ORIG_ODS_NAME
_FLOORSTAND_MATRIX_ODS = _FLOORSTAND_DIR / "fixtures" / FLOORSTAND_MATRIX_ODS_NAME
_FLOORSTAND_BUDGET_ODS = _FLOORSTAND_DIR / "fixtures" / FLOORSTAND_BUDGET_ODS_NAME
_CALC_PRIMARY_ODS = _CALC_PRIMARY_DIR / "fixtures" / RAW_DATA_ODS_NAME
_REVERSE_CBA_ODT = _REVERSE_DIR / "fixtures" / CBA_EXCERPT_ODT_NAME
_REVERSE_ROSTER_XLSX = _REVERSE_DIR / "fixtures" / ROSTER_XLSX_NAME
_LONG_FACTS_ODT = _LONG_DIR / "fixtures" / LONG_FACTS_ODT_NAME
_LONG_DECISIONS_ODT = _LONG_DIR / "fixtures" / LONG_DECISIONS_ODT_NAME
_DRAW_PRIMARY_ODG = _DRAW_DIR / "fixtures" / DRAW_PRIMARY_ODG_NAME
DEFAULT_TRIAL_DIR_NAME = "writeragent-eval2-afc"
DEFAULT_TENANT_TRIAL_DIR_NAME = "writeragent-eval2-tenant"
DEFAULT_CADAVER_TRIAL_DIR_NAME = "writeragent-eval2-cadaver"
DEFAULT_GMP_TRIAL_DIR_NAME = "writeragent-eval2-gmp"
DEFAULT_FLOORSTAND_TRIAL_DIR_NAME = "writeragent-eval2-writer-calc"
DEFAULT_CALC_PRIMARY_TRIAL_DIR_NAME = "writeragent-eval2-calc-primary"
DEFAULT_REVERSE_TRIAL_DIR_NAME = "writeragent-eval2-reverse-tenant"
DEFAULT_LONG_TRIAL_DIR_NAME = "writeragent-eval2-long-writer"
DEFAULT_DRAW_TRIAL_DIR_NAME = "writeragent-eval2-draw"
DEFAULT_PYTHON_SHAPES_TRIAL_DIR_NAME = "writeragent-eval2-python-shapes-flag"
TASK_AFC = "afc"
TASK_TENANT = "tenant-retention"
TASK_CADAVER = "cadaver-proposal"
TASK_GMP = "gmp-change-control"
TASK_WRITER_CALC = "writer-calc-peer-write"
TASK_CALC_PRIMARY = "calc-primary-model"
TASK_REVERSE = "reverse-tenant"
TASK_LONG = "long-writer-pack"
TASK_DRAW = "draw-primary"
TASK_PYTHON_SHAPES = "python-shapes-flag"
TASK_CHOICES = (
    TASK_AFC,
    TASK_TENANT,
    TASK_CADAVER,
    TASK_GMP,
    TASK_WRITER_CALC,
    TASK_CALC_PRIMARY,
    TASK_REVERSE,
    TASK_LONG,
    TASK_DRAW,
    TASK_PYTHON_SHAPES,
)


def writeragent_json_candidates() -> list[Path]:
    """Same profile locations as bench_embeddings / strip_lru / the debug log."""
    return [directory / "writeragent.json" for directory in lo_user_profile_dirs()]


def find_writeragent_json(
    explicit: Path | None = None,
    candidates: list[Path] | None = None,
) -> Path:
    if explicit is not None:
        return explicit
    search = writeragent_json_candidates() if candidates is None else candidates
    for path in search:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Could not find writeragent.json. Pass --config PATH "
        "(LibreOffice user profile)."
    )


def _split_comment_header(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].lstrip(" \t")
        if stripped == "" or stripped.startswith("//"):
            idx += 1
            continue
        break
    return "".join(lines[:idx]), "".join(lines[idx:])


def parse_config_object(text: str) -> dict[str, Any]:
    _header, body = _split_comment_header(text)
    data = json.loads(body or "{}")
    if not isinstance(data, dict):
        raise ValueError("writeragent.json must be a JSON object")
    return data


def read_max_tool_rounds(data: dict[str, Any]) -> int:
    """Effective cap: missing key is the schema default (15)."""
    if MAX_TOOL_ROUNDS_KEY not in data:
        return DEFAULT_MAX_TOOL_ROUNDS
    return int(data[MAX_TOOL_ROUNDS_KEY])


def apply_max_tool_rounds(data: dict[str, Any], rounds: int) -> object | None:
    """Write rounds onto data. Return the prior raw value, or None if absent."""
    previous = data[MAX_TOOL_ROUNDS_KEY] if MAX_TOOL_ROUNDS_KEY in data else None
    data[MAX_TOOL_ROUNDS_KEY] = rounds
    return previous


def restore_max_tool_rounds(data: dict[str, Any], previous: object | None) -> None:
    """Put back the prior value, or drop the key if it was not set."""
    if previous is None:
        data.pop(MAX_TOOL_ROUNDS_KEY, None)
    else:
        data[MAX_TOOL_ROUNDS_KEY] = previous


def write_config_flushed(path: Path, data: dict[str, Any], header: str = "") -> None:
    body = json.dumps(data, indent=4) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        if header:
            handle.write(header)
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def temporary_max_tool_rounds(
    config_path: Path,
    rounds: int = EVAL_2_MAX_TOOL_ROUNDS,
):
    """Set chatbot.max_tool_rounds, then restore the previous value (or omit the key)."""
    existed = config_path.is_file()
    text = config_path.read_text(encoding="utf-8") if existed else "{}"
    header = _split_comment_header(text)[0]
    data = parse_config_object(text)
    previous = apply_max_tool_rounds(data, rounds)
    write_config_flushed(config_path, data, header)
    try:
        yield previous
    finally:
        restore_max_tool_rounds(data, previous)
        if not existed and previous is None and not data:
            if config_path.is_file():
                config_path.unlink()
        else:
            write_config_flushed(config_path, data, header)


def find_afc_fixture() -> Path | None:
    for path in _FIXTURE_CANDIDATES:
        if path.is_file():
            return path
    return None


def find_afc_population_ods() -> Path:
    """Population ODS only. XLSX stay in fixtures/ (sibling, researchable)."""
    if _FIXTURE_ODS.is_file():
        return _FIXTURE_ODS
    raise FileNotFoundError(
        f"Missing {_FIXTURE_ODS}. Convert the xlsx fixture with "
        "soffice --convert-to ods; do not open fixtures/ (siblings leak)."
    )


def task_max_tool_rounds(task: str) -> int:
    """Headed start: 150 for GMP / Floorstand / reverse-tenant / calc-primary."""
    if task == TASK_CALC_PRIMARY:
        return EVAL_2_CALC_PRIMARY_MAX_TOOL_ROUNDS
    if task in {TASK_GMP, TASK_WRITER_CALC, TASK_REVERSE}:
        return EVAL_2_GMP_MAX_TOOL_ROUNDS
    return EVAL_2_MAX_TOOL_ROUNDS


def task_doc_dir(task: str) -> Path:
    """Task folder under docs/eval/eval-2/ (holds fixtures/ + runs/)."""
    return {
        TASK_AFC: _AFC_DIR,
        TASK_TENANT: _TENANT_DIR,
        TASK_CADAVER: _CADAVER_DIR,
        TASK_GMP: _GMP_DIR,
        TASK_WRITER_CALC: _FLOORSTAND_DIR,
        TASK_CALC_PRIMARY: _CALC_PRIMARY_DIR,
        TASK_REVERSE: _REVERSE_DIR,
        TASK_LONG: _LONG_DIR,
        TASK_DRAW: _DRAW_DIR,
        TASK_PYTHON_SHAPES: _PYTHON_SHAPES_DIR,
    }[task]


def default_eval2_run_dir(task: str, *, now: datetime | None = None) -> Path:
    """Durable stamp dir for headed artifacts (debug log, later notes/ods)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    return task_doc_dir(task) / "runs" / stamp


def resolve_headed_run_dir(task: str, run_dir: Path | None, *, now: datetime | None = None) -> Path:
    if run_dir is not None:
        return run_dir
    return default_eval2_run_dir(task, now=now)


def default_eval2_trial_dir(task: str = TASK_AFC) -> Path:
    names = {
        TASK_TENANT: DEFAULT_TENANT_TRIAL_DIR_NAME,
        TASK_CADAVER: DEFAULT_CADAVER_TRIAL_DIR_NAME,
        TASK_GMP: DEFAULT_GMP_TRIAL_DIR_NAME,
        TASK_WRITER_CALC: DEFAULT_FLOORSTAND_TRIAL_DIR_NAME,
        TASK_CALC_PRIMARY: DEFAULT_CALC_PRIMARY_TRIAL_DIR_NAME,
        TASK_REVERSE: DEFAULT_REVERSE_TRIAL_DIR_NAME,
        TASK_LONG: DEFAULT_LONG_TRIAL_DIR_NAME,
        TASK_DRAW: DEFAULT_DRAW_TRIAL_DIR_NAME,
        TASK_PYTHON_SHAPES: DEFAULT_PYTHON_SHAPES_TRIAL_DIR_NAME,
    }
    name = names.get(task, DEFAULT_TRIAL_DIR_NAME)
    return Path(tempfile.gettempdir()) / name


def _task_dirs() -> tuple[Path, ...]:
    return (
        _AFC_DIR.resolve(),
        _TENANT_DIR.resolve(),
        _CADAVER_DIR.resolve(),
        _GMP_DIR.resolve(),
        _FLOORSTAND_DIR.resolve(),
        _CALC_PRIMARY_DIR.resolve(),
        _REVERSE_DIR.resolve(),
        _LONG_DIR.resolve(),
        _DRAW_DIR.resolve(),
        _PYTHON_SHAPES_DIR.resolve(),
    )


def _is_protected_trial_dest(dest_dir: Path, source: Path) -> bool:
    """Refuse dest that would wipe the task tree, fixtures, or a filesystem root."""
    dest_dir = dest_dir.resolve()
    source = source.resolve()
    if dest_dir == source.parent or dest_dir in source.parents:
        return True
    for task_dir in _task_dirs():
        if dest_dir == task_dir or dest_dir == task_dir.parent:
            return True
        try:
            dest_dir.relative_to(task_dir)
            return True
        except ValueError:
            pass
    if dest_dir == Path(dest_dir.anchor) or dest_dir == Path(tempfile.gettempdir()):
        return True
    return False


def _wipe_dir(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for child in dest_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_blank_writer_odt(path: Path) -> Path:
    """Minimal empty Writer document so the open memo lives in the trial dir."""
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">
 <office:body><office:text/></office:body>
</office:document-content>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("content.xml", content)
    return path


def write_blank_calc_ods(path: Path) -> Path:
    """Minimal empty Calc workbook so the open Theatre CBA file lives in the trial dir."""
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" office:version="1.2">
 <office:body><office:spreadsheet>
  <table:table table:name="Sheet1"><table:table-row><table:table-cell/></table:table-row></table:table>
 </office:spreadsheet></office:body>
</office:document-content>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.spreadsheet",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("content.xml", content)
    return path


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_gmp_change_control_odg(path: Path) -> Path:
    """Editable Draw Form-920 stand-in: labels + empty named text boxes.

    Not the gold PDF. Empty ``fld_*`` frames are fill_draw_fields targets.
    """
    frames: list[str] = []
    frames.append(
        '<draw:frame draw:name="lbl_form_header" svg:x="1cm" svg:y="0.4cm" '
        'svg:width="19cm" svg:height="1.1cm"><draw:text-box>'
        "<text:p>Change Control Tracking Form (Form-920 stand-in)</text:p>"
        "</draw:text-box></draw:frame>"
    )
    y = 1.8
    for name, label in GMP_FILLABLE_FIELDS:
        tall = 2.2 if name in {
            "fld_current_situation",
            "fld_proposed_situation",
            "fld_justification",
            "fld_risk_comments",
        } else 1.1
        frames.append(
            f'<draw:frame draw:name="lbl_{name}" svg:x="1cm" svg:y="{y:.1f}cm" '
            f'svg:width="6.4cm" svg:height="{tall:.1f}cm"><draw:text-box>'
            f"<text:p>{_xml_escape(label)}</text:p>"
            "</draw:text-box></draw:frame>"
        )
        frames.append(
            f'<draw:frame draw:name="{_xml_escape(name)}" svg:x="7.6cm" '
            f'svg:y="{y:.1f}cm" svg:width="12.4cm" svg:height="{tall:.1f}cm">'
            "<draw:text-box><text:p/></draw:text-box></draw:frame>"
        )
        y += tall + 0.25
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:version="1.2">\n'
        " <office:automatic-styles/>\n"
        " <office:body><office:drawing>"
        '<draw:page draw:name="ChangeControl" draw:master-page-name="Standard">'
        + "".join(frames)
        + "</draw:page></office:drawing></office:body>\n"
        "</office:document-content>\n"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'office:version="1.2">\n'
        " <office:master-styles>"
        '<style:master-page style:name="Standard"/>'
        "</office:master-styles>\n"
        "</office:document-styles>\n"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n'
        ' <manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        'manifest:media-type="application/vnd.oasis.opendocument.graphics"/>\n'
        ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
        ' <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>\n'
        "</manifest:manifest>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.graphics",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("styles.xml", styles)
        zf.writestr("content.xml", content)
    return path


def _write_odg_zip(path: Path, *, page_name: str, body_inner: str) -> Path:
    """Minimal Draw package. Same zip shape as the GMP Form-920 stand-in."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:version="1.2">\n'
        " <office:automatic-styles/>\n"
        " <office:body><office:drawing>"
        f'<draw:page draw:name="{_xml_escape(page_name)}" draw:master-page-name="Standard">'
        + body_inner
        + "</draw:page></office:drawing></office:body>\n"
        "</office:document-content>\n"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'office:version="1.2">\n'
        " <office:master-styles>"
        '<style:master-page style:name="Standard"/>'
        "</office:master-styles>\n"
        "</office:document-styles>\n"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n'
        ' <manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        'manifest:media-type="application/vnd.oasis.opendocument.graphics"/>\n'
        ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
        ' <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>\n'
        "</manifest:manifest>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.graphics",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("styles.xml", styles)
        zf.writestr("content.xml", content)
    return path


def write_draw_primary_odg(path: Path) -> Path:
    """Editable Draw process-map canvas. Not the gold PDF.

    Title-only husk so headed fill/build starts from a real ``.odg``.
    Process steps stay empty — the model builds labeled shapes here.
    """
    title = (
        '<draw:frame draw:name="title" svg:x="1cm" svg:y="0.4cm" '
        'svg:width="26cm" svg:height="1.2cm"><draw:text-box>'
        "<text:p>Process Flow Map</text:p>"
        "</draw:text-box></draw:frame>"
    )
    return _write_odg_zip(path, page_name="ProcessFlow", body_inner=title)


def write_floorstand_budget_ods(path: Path) -> Path:
    """Empty two-tab Calc scaffold. Titles only — no invented figures.

    Not the gold deliverable. Cost Comparison + Final Store List are the
    write targets; store-list refs stay research-only beside this file.
    """
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'office:version="1.2">\n'
        " <office:body><office:spreadsheet>\n"
        f'  <table:table table:name="{_xml_escape(FLOORSTAND_COST_SHEET)}">'
        '<table:table-row><table:table-cell office:value-type="string">'
        "<text:p>Holiday Floorstand Budget</text:p>"
        "</table:table-cell></table:table-row></table:table>\n"
        f'  <table:table table:name="{_xml_escape(FLOORSTAND_STORE_SHEET)}">'
        '<table:table-row><table:table-cell office:value-type="string">'
        "<text:p>Final store list</text:p>"
        "</table:table-cell></table:table-row></table:table>\n"
        " </office:spreadsheet></office:body>\n"
        "</office:document-content>\n"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n'
        ' <manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>\n'
        ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
        "</manifest:manifest>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.spreadsheet",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("content.xml", content)
    return path


def stage_clean_trial_files(sources: list[Path], dest_dir: Path, *, label: str) -> list[Path]:
    """Copy only *sources* into *dest_dir* (wiped). Prompt/rubric/gold stay outside."""
    dest_dir = dest_dir.resolve()
    resolved: list[Path] = []
    for source in sources:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"{label} fixture not found: {source}")
        if _is_protected_trial_dest(dest_dir, source):
            raise ValueError(
                f"refusing to stage into protected path {dest_dir} "
                "(task tree, fixture folder, or filesystem/temp root)"
            )
        resolved.append(source)
    _wipe_dir(dest_dir)
    copied: list[Path] = []
    for source in resolved:
        dest = dest_dir / source.name
        shutil.copy2(source, dest)
        copied.append(dest)
    return copied


def stage_clean_trial_ods(source: Path, dest_dir: Path) -> Path:
    """Copy only *source* into *dest_dir* (wiped). Prompt/rubric/gold stay outside.

    document_research lists the open workbook's folder. Opening from fixtures/
    or the AFC task dir exposes xlsx, min-range ODS, prompt.txt, rubric, notes.
    """
    copied = stage_clean_trial_files([source], dest_dir, label="Population")
    return copied[0]


def find_tenant_fixtures() -> tuple[Path, Path]:
    if not _TENANT_LETTER_ODT.is_file():
        raise FileNotFoundError(
            f"Missing {_TENANT_LETTER_ODT}. Convert the letter fixture; "
            "do not open fixtures/ (siblings leak)."
        )
    if not _TENANT_SURVEY_XLSX.is_file():
        raise FileNotFoundError(f"Missing {_TENANT_SURVEY_XLSX}")
    return _TENANT_LETTER_ODT, _TENANT_SURVEY_XLSX


def stage_tenant_trial(dest_dir: Path) -> Path:
    """Letter + survey + blank memo. Gold/prompt stay outside the trial dir."""
    letter, survey = find_tenant_fixtures()
    stage_clean_trial_files([letter, survey], dest_dir, label="Tenant")
    return write_blank_writer_odt(dest_dir / TENANT_MEMO_NAME)


def find_cadaver_budget() -> Path:
    if not _CADAVER_BUDGET_XLSX.is_file():
        raise FileNotFoundError(
            f"Missing {_CADAVER_BUDGET_XLSX}. Convert the budget fixture; "
            "do not open fixtures/ (siblings leak)."
        )
    return _CADAVER_BUDGET_XLSX


def stage_cadaver_trial(dest_dir: Path) -> Path:
    """Budget xlsx + blank proposal. Gold/prompt stay outside the trial dir."""
    budget = find_cadaver_budget()
    stage_clean_trial_files([budget], dest_dir, label="Cadaver")
    return write_blank_writer_odt(dest_dir / CADAVER_PROPOSAL_NAME)


def find_gmp_fixtures() -> tuple[Path, Path, Path]:
    """COA PDF + spec ODT + Draw stand-in. Gold PDF is not a write target."""
    if not _GMP_COA_PDF.is_file():
        raise FileNotFoundError(f"Missing {_GMP_COA_PDF}")
    if not _GMP_SPEC_ODT.is_file():
        raise FileNotFoundError(
            f"Missing {_GMP_SPEC_ODT}. Convert the spec fixture; "
            "do not open fixtures/ (siblings leak)."
        )
    if not _GMP_FORM_ODG.is_file():
        raise FileNotFoundError(
            f"Missing {_GMP_FORM_ODG}. Rebuild with write_gmp_change_control_odg."
        )
    return _GMP_COA_PDF, _GMP_SPEC_ODT, _GMP_FORM_ODG


def stage_gmp_trial(dest_dir: Path) -> tuple[Path, Path]:
    """COA + spec + Draw stand-in + blank memo. Gold PDF/prompt stay outside."""
    coa, spec, form = find_gmp_fixtures()
    stage_clean_trial_files([coa, spec, form], dest_dir, label="GMP")
    memo = write_blank_writer_odt(dest_dir / GMP_MEMO_NAME)
    return memo, dest_dir / GMP_FORM_ODG_NAME


def find_floorstand_fixtures() -> tuple[Path, Path, Path, Path]:
    """Email trail + two store lists + budget scaffold. Gold xlsx is not staged."""
    if not _FLOORSTAND_EMAIL_TRAIL_ODT.is_file():
        raise FileNotFoundError(
            f"Missing {_FLOORSTAND_EMAIL_TRAIL_ODT}. Convert the email trail; "
            "do not open fixtures/ (siblings leak)."
        )
    if not _FLOORSTAND_ORIG_ODS.is_file():
        raise FileNotFoundError(f"Missing {_FLOORSTAND_ORIG_ODS}")
    if not _FLOORSTAND_MATRIX_ODS.is_file():
        raise FileNotFoundError(f"Missing {_FLOORSTAND_MATRIX_ODS}")
    if not _FLOORSTAND_BUDGET_ODS.is_file():
        raise FileNotFoundError(
            f"Missing {_FLOORSTAND_BUDGET_ODS}. Rebuild with write_floorstand_budget_ods."
        )
    return (
        _FLOORSTAND_EMAIL_TRAIL_ODT,
        _FLOORSTAND_ORIG_ODS,
        _FLOORSTAND_MATRIX_ODS,
        _FLOORSTAND_BUDGET_ODS,
    )


def stage_floorstand_trial(dest_dir: Path) -> tuple[Path, Path]:
    """Refs + scaffold workbook + blank email. Gold/prompt stay outside."""
    trail, orig, matrix, budget = find_floorstand_fixtures()
    stage_clean_trial_files([trail, orig, matrix, budget], dest_dir, label="Floorstand")
    email = write_blank_writer_odt(dest_dir / FLOORSTAND_EMAIL_NAME)
    return email, dest_dir / FLOORSTAND_BUDGET_ODS_NAME


def find_calc_primary_ods() -> Path:
    """Raw Data ODS only. XLSX stays in fixtures/ (sibling, researchable)."""
    if _CALC_PRIMARY_ODS.is_file():
        return _CALC_PRIMARY_ODS
    raise FileNotFoundError(
        f"Missing {_CALC_PRIMARY_ODS}. Convert the xlsx fixture with "
        "soffice --convert-to ods; do not open fixtures/ (siblings leak)."
    )


def stage_calc_primary_trial(dest_dir: Path) -> Path:
    """Copy only the Raw Data ODS. Prompt/rubric/gold stay outside the trial dir."""
    return stage_clean_trial_ods(find_calc_primary_ods(), dest_dir)


def find_reverse_tenant_fixtures() -> tuple[Path, Path]:
    """CBA excerpt ODT + sample roster XLSX. Writer brief is research-only."""
    if not _REVERSE_CBA_ODT.is_file():
        raise FileNotFoundError(
            f"Missing {_REVERSE_CBA_ODT}. Convert the CBA excerpt fixture; "
            "do not open fixtures/ (siblings leak)."
        )
    if not _REVERSE_ROSTER_XLSX.is_file():
        raise FileNotFoundError(f"Missing {_REVERSE_ROSTER_XLSX}")
    return _REVERSE_CBA_ODT, _REVERSE_ROSTER_XLSX


def stage_reverse_tenant_trial(dest_dir: Path) -> tuple[Path, Path]:
    """Brief ODT + roster xlsx + blank Theatre CBA. Gold/prompt stay outside."""
    brief, roster = find_reverse_tenant_fixtures()
    stage_clean_trial_files([brief, roster], dest_dir, label="Reverse Tenant")
    workbook = write_blank_calc_ods(dest_dir / THEATRE_CBA_ODS_NAME)
    return workbook, dest_dir / CBA_EXCERPT_ODT_NAME


def find_long_writer_fixtures() -> tuple[Path, Path]:
    """Program facts + decision log. Both are research / read-only."""
    if not _LONG_FACTS_ODT.is_file():
        raise FileNotFoundError(f"Missing {_LONG_FACTS_ODT}")
    if not _LONG_DECISIONS_ODT.is_file():
        raise FileNotFoundError(f"Missing {_LONG_DECISIONS_ODT}")
    return _LONG_FACTS_ODT, _LONG_DECISIONS_ODT


def stage_long_writer_trial(dest_dir: Path) -> Path:
    """Facts + decision log + blank brief. Prompt/notes stay outside."""
    facts, decisions = find_long_writer_fixtures()
    stage_clean_trial_files([facts, decisions], dest_dir, label="Long Writer")
    return write_blank_writer_odt(dest_dir / LONG_PACK_NAME)


def find_draw_primary_fixture() -> Path:
    """Editable Draw canvas. Gold Process Flow Map.pdf is not a write target."""
    if not _DRAW_PRIMARY_ODG.is_file():
        raise FileNotFoundError(
            f"Missing {_DRAW_PRIMARY_ODG}. Rebuild with write_draw_primary_odg."
        )
    return _DRAW_PRIMARY_ODG


def stage_draw_primary_trial(dest_dir: Path) -> Path:
    """Draw stand-in only. Gold PDF / prompt / rubric stay outside the trial dir."""
    canvas = find_draw_primary_fixture()
    copied = stage_clean_trial_files([canvas], dest_dir, label="Draw-primary")
    return copied[0]


def stage_python_shapes_flag_trial(dest_dir: Path) -> Path:
    """Blank Writer canvas only. Prompt / rubric stay outside the trial dir.

    No research fixture: the agent draws the flag on this document's draw
    page. Opening the task folder would let folder listing see the rubric.
    """
    dest_dir = dest_dir.resolve()
    marker = _PYTHON_SHAPES_DIR / "prompt.writeragent.txt"
    if _is_protected_trial_dest(dest_dir, marker):
        raise ValueError(
            f"refusing to stage into protected path {dest_dir} "
            "(task tree, fixture folder, or filesystem/temp root)"
        )
    _wipe_dir(dest_dir)
    return write_blank_writer_odt(dest_dir / PYTHON_SHAPES_ODT_NAME)


def snapshot_trial_document(src: Path, dest_dir: Path, dest_name: str) -> Path | None:
    """Copy the headed trial document into the stamp dir.

    That file is what ``--score`` reads, including the page thumbnail
    LibreOffice stored inside it. Missing or unreadable sources warn and
    return None so config restore still runs.
    """
    if not src.is_file():
        print(f"Warning: trial document missing, no preview source: {src}", file=sys.stderr)
        return None
    try:
        dest_dir = dest_dir.expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / dest_name
        if dest.resolve() != src.resolve():
            shutil.copy2(src, dest)
    except OSError as exc:
        print(f"Warning: could not snapshot trial document: {exc}", file=sys.stderr)
        return None
    print(f"Saved {src.name} -> {dest}")
    return dest


def launch_office(mode: str, fixture: Path | None) -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        print("soffice not on PATH; open the document yourself.", file=sys.stderr)
        return
    flags = {"writer": "--writer", "calc": "--calc", "draw": "--draw"}
    flag = flags.get(mode, "--calc")
    cmd = [soffice, flag]
    if fixture is not None:
        cmd.append(str(fixture))
    subprocess.Popen(cmd)


def launch_calc(fixture: Path | None) -> None:
    launch_office("calc", fixture)


def launch_office_documents(paths: list[Path]) -> None:
    """Open several files in one soffice process (Writer + Draw pre-open)."""
    soffice = shutil.which("soffice")
    if soffice is None:
        print("soffice not on PATH; open the documents yourself.", file=sys.stderr)
        return
    cmd = [soffice, *[str(path) for path in paths]]
    subprocess.Popen(cmd)


def _wait_for_finish(rounds: int = EVAL_2_MAX_TOOL_ROUNDS) -> None:
    prompt = (
        f"set to {rounds}; Ctrl-C / Enter to restore "
        f"{MAX_TOOL_ROUNDS_KEY}.\n"
    )
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="writeragent.json path")
    parser.add_argument(
        "--task",
        choices=TASK_CHOICES,
        default=TASK_AFC,
        help=(
            "Experiment to launch or score (default: afc). "
            "tenant-retention / cadaver-proposal / long-writer-pack are Writer; "
            "gmp-change-control is Writer+Draw; writer-calc-peer-write is "
            "Writer+Calc; reverse-tenant is Calc (Writer brief is a read); "
            "calc-primary-model is Calc; draw-primary is Draw; "
            "python-shapes-flag is Writer (python domain → shapes)."
        ),
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help=(
            "Stage a clean trial dir, then soffice (Calc for AFC / "
            "calc-primary-model, Writer for tenant-retention / "
            "cadaver-proposal / long-writer-pack, Writer+Draw for "
            "gmp-change-control, Writer+Calc for writer-calc-peer-write, "
            "Writer brief + Calc for reverse-tenant, Draw for draw-primary, "
            "blank Writer for python-shapes-flag)"
        ),
    )
    parser.add_argument(
        "--trial-dir",
        type=Path,
        default=None,
        help=(
            "Directory that will contain only the staged refs "
            f"(default: $TMP/{DEFAULT_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_TENANT_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_CADAVER_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_GMP_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_FLOORSTAND_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_CALC_PRIMARY_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_REVERSE_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_LONG_TRIAL_DIR_NAME}, "
            f"$TMP/{DEFAULT_DRAW_TRIAL_DIR_NAME}, or "
            f"$TMP/{DEFAULT_PYTHON_SHAPES_TRIAL_DIR_NAME})"
        ),
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Durable stamp dir that receives writeragent_debug.log on "
            "--launch exit (typically docs/eval/eval-2/<task>/runs/<stamp>). "
            "Default: auto-stamp YYYYMMDD-HHMM under that task's runs/"
        ),
    )
    parser.add_argument(
        "--score",
        type=Path,
        default=None,
        help="Score a saved trial artifact. Ignores chat Ready; does not write config.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Optional command to run as the session (prefix with --)",
    )
    args = parser.parse_args(argv)
    if args.score is not None:
        suffix = args.score.suffix.lower()
        if args.task == TASK_WRITER_CALC:
            from eval_2_floorstand_oracle import main as score_main
        elif args.task == TASK_GMP:
            from eval_2_gmp_oracle import main as score_main
        elif args.task == TASK_REVERSE:
            from eval_2_reverse_tenant_oracle import main as score_main
        elif args.task == TASK_CALC_PRIMARY:
            from eval_2_calc_primary_oracle import main as score_main
        elif args.task == TASK_LONG:
            from eval_2_long_writer_oracle import main as score_main
        elif args.task == TASK_DRAW:
            from eval_2_draw_oracle import main as score_main
        elif args.task == TASK_PYTHON_SHAPES:
            from eval_2_python_shapes_flag_oracle import main as score_main
        elif args.task == TASK_CADAVER:
            from eval_2_cadaver_oracle import main as score_main
        elif args.task == TASK_TENANT or suffix in {".odt", ".docx"}:
            from eval_2_tenant_oracle import main as score_main
        else:
            from eval_2_ods_oracle import main as score_main

        return score_main([str(args.score)])
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    config_path = find_writeragent_json(args.config)
    exit_code = 0
    rounds = task_max_tool_rounds(args.task)
    with temporary_max_tool_rounds(config_path, rounds):
        print(f"Using {config_path}: {MAX_TOOL_ROUNDS_KEY}={rounds}")
        if command:
            exit_code = subprocess.call(command)
        elif args.launch:
            trial_dir = args.trial_dir or default_eval2_trial_dir(args.task)
            preview_src: Path | None = None
            try:
                if args.task == TASK_TENANT:
                    trial_doc = stage_tenant_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in trial_doc.parent.iterdir()))
                    print(f"Staged clean trial dir {trial_doc.parent} ({staged})")
                    launch_office("writer", trial_doc)
                elif args.task == TASK_CADAVER:
                    trial_doc = stage_cadaver_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in trial_doc.parent.iterdir()))
                    print(f"Staged clean trial dir {trial_doc.parent} ({staged})")
                    launch_office("writer", trial_doc)
                elif args.task == TASK_GMP:
                    memo, form = stage_gmp_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in memo.parent.iterdir()))
                    print(f"Staged clean trial dir {memo.parent} ({staged})")
                    print(
                        "Pre-open: Draw form then Writer memo. "
                        "Open the Draw sidebar once before START. "
                        "Gold PDF is not the write target."
                    )
                    # Form first, memo last so Writer is the focused chat doc.
                    launch_office_documents([form, memo])
                elif args.task == TASK_WRITER_CALC:
                    email, budget = stage_floorstand_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in email.parent.iterdir()))
                    print(f"Staged clean trial dir {email.parent} ({staged})")
                    print(
                        "Pre-open: Calc budget then Writer email. "
                        "Open the Calc sidebar once before START. "
                        "Gold xlsx is not the write target."
                    )
                    # Workbook first, email last so Writer is the focused chat doc.
                    launch_office_documents([budget, email])
                elif args.task == TASK_CALC_PRIMARY:
                    trial_ods = stage_calc_primary_trial(trial_dir)
                    print(f"Staged clean trial dir {trial_ods.parent} ({trial_ods.name} only)")
                    launch_calc(trial_ods)
                elif args.task == TASK_REVERSE:
                    workbook, brief = stage_reverse_tenant_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in workbook.parent.iterdir()))
                    print(f"Staged clean trial dir {workbook.parent} ({staged})")
                    print(
                        "Pre-open: Writer CBA excerpt then Calc Theatre CBA. "
                        "Paste the prompt in the Calc sidebar. "
                        "The Writer excerpt is research / read-only."
                    )
                    # Brief first, workbook last so Calc is the focused chat doc.
                    launch_office_documents([brief, workbook])
                elif args.task == TASK_LONG:
                    trial_doc = stage_long_writer_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in trial_doc.parent.iterdir()))
                    print(f"Staged clean trial dir {trial_doc.parent} ({staged})")
                    launch_office("writer", trial_doc)
                elif args.task == TASK_DRAW:
                    trial_doc = stage_draw_primary_trial(trial_dir)
                    staged = ", ".join(sorted(p.name for p in trial_doc.parent.iterdir()))
                    print(f"Staged clean trial dir {trial_doc.parent} ({staged})")
                    print(
                        "Pre-open: Draw canvas only. Open the Draw sidebar. "
                        "Gold PDF is not the write target. Writer is optional/absent."
                    )
                    launch_office("draw", trial_doc)
                elif args.task == TASK_PYTHON_SHAPES:
                    trial_doc = stage_python_shapes_flag_trial(trial_dir)
                    preview_src = trial_doc
                    staged = ", ".join(sorted(p.name for p in trial_doc.parent.iterdir()))
                    print(f"Staged clean trial dir {trial_doc.parent} ({staged})")
                    print(
                        "Pre-open: blank Writer. Paste the Ask in the Writer sidebar. "
                        "Shapes go on this document's draw page. "
                        "Save in Writer before Enter so the stamp keeps final_flag.odt."
                    )
                    launch_office("writer", trial_doc)
                else:
                    trial_ods = stage_clean_trial_ods(
                        find_afc_population_ods(),
                        trial_dir,
                    )
                    print(f"Staged clean trial dir {trial_ods.parent} ({trial_ods.name} only)")
                    launch_calc(trial_ods)
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            _wait_for_finish(rounds)
            # Copy before the next trial's LO restart resets the live log.
            run_dir = resolve_headed_run_dir(args.task, args.run_dir)
            snapshot_debug_log(run_dir)
            if preview_src is not None:
                snapshot_trial_document(preview_src, run_dir, PYTHON_SHAPES_STAMP_ODT)
        else:
            _wait_for_finish(rounds)
    print(f"Restored previous {MAX_TOOL_ROUNDS_KEY} in {config_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
